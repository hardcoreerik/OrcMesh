"""MeshChat – WaterfallView: RTL-SDR spectrum waterfall display.

The rendering side is hardware-independent: feed it FFT power rows via
`push_row()` and it scrolls a waterfall. The SDR capture backend
(`sdr_source.py`) supplies those rows when a dongle is present.
"""
from __future__ import annotations

import logging

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

log = logging.getLogger(__name__)

# pyqtgraph's default ImageItem axis order is 'col-major' (data[x, y] —
# axis 0 maps to the X screen coordinate). _history below is built as
# (time_samples, freq_bins) — axis 0 = time, axis 1 = frequency — which is
# 'row-major' semantics (data[y, x], matching plain numpy/image
# conventions). Without this, the waterfall image renders with time
# horizontal and frequency vertical while everything else (axis labels,
# setRect's frequency-based X range, and set_markers()'s frequency-based
# vertical LinearRegionItems) assumes the opposite — the actual spectral
# content and the channel markers end up completely misaligned.
pg.setConfigOptions(imageAxisOrder="row-major")

# Cyan→purple→amber ramp matching the app's deep-space theme
_COLORMAP_STOPS = [
    (0.00, (7, 13, 31)),        # near-black navy (noise floor)
    (0.35, (0, 80, 140)),       # deep blue
    (0.55, (0, 212, 255)),      # cyan
    (0.75, (139, 92, 246)),     # purple
    (1.00, (255, 184, 0)),      # amber (strong signal)
]

_HISTORY_ROWS = 300

#: Display range when the view is left to fit itself. Percentiles rather than
#: min/max, so one loud bin cannot flatten everything else into a single colour.
AUTO_LOW_PERCENTILE = 5.0
AUTO_HIGH_PERCENTILE = 99.0

#: A quiet band has almost no spread — a few dB at most — and fitting a window
#: that tight would amplify its noise into structure that is not there. This is
#: the narrowest window auto-fit will use.
MIN_LEVELS_SPAN_DB = 10.0

#: Rows between auto re-fits. Fitting every row would make the picture breathe
#: as the window chased the noise; this is about four times a second.
_AUTO_REFIT_ROWS = 12

#: A re-fit only happens if it moves the range by at least this much.
_REFIT_EPSILON_DB = 0.5


def build_colormap() -> pg.ColorMap:
    """The shared spectrum ramp, used by both waterfalls."""
    positions = np.array([s[0] for s in _COLORMAP_STOPS])
    colors = np.array([s[1] for s in _COLORMAP_STOPS], dtype=np.ubyte)
    return pg.ColorMap(positions, colors)


class WaterfallView(QWidget):
    """Scrolling spectrum waterfall. Newest row appears at the bottom."""

    #: (low_db, high_db) whenever the display range changes.
    levels_changed = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Frequency", units="Hz")
        self._plot.setLabel("left", "Time")
        self._plot.getPlotItem().invertY(True)
        self._plot.setMouseEnabled(x=True, y=False)
        layout.addWidget(self._plot)

        self._image = pg.ImageItem()
        self._image.setColorMap(build_colormap())
        self._plot.addItem(self._image)

        self._bins = 0
        self._history: np.ndarray | None = None
        self._center_hz = 0.0
        self._span_hz = 0.0
        self._marker_items: list = []
        #: Auto-fit by default. A fixed range only works if it happens to match
        #: the dB scale of whatever feeds the view, and it did not: everything
        #: landed past the top of the colormap and the whole panel went amber.
        self._auto_levels = True
        self._levels = (-60.0, 10.0)
        self._refit_countdown = 0
        self._fits_done = 0

    # ------------------------------------------------------------------

    def configure(self, center_hz: float, span_hz: float, bins: int) -> None:
        """Reset the waterfall for a new capture geometry."""
        self._center_hz = center_hz
        self._span_hz = span_hz
        self._bins = bins
        # NaN, not zero: an unwritten row is not a measurement of 0 dB, and
        # starting from zeros made the first fit span from 0 up to the real data
        # — a window dominated by rows that had never been measured. pyqtgraph
        # renders NaN as the bottom of the ramp, which is what "no data" should
        # look like.
        self._history = np.full((_HISTORY_ROWS, bins), np.nan, dtype=np.float32)
        self._image.setImage(self._history, autoLevels=False)
        # Unconditionally, before anything can paint. `setImage(autoLevels=False)`
        # leaves levels unset, and pyqtgraph refuses to render float data without
        # them: "levels argument is required for float input types". Leaving this
        # to the auto-fit meant the very first paint raised, and the crash surfaced
        # in the window rather than in any test that never painted.
        self._image.setLevels(self._levels)
        # Fit again from scratch: a different capture may be at a different gain,
        # so the previous window is meaningless.
        self._refit_countdown = 0
        self._fits_done = 0

        start_hz = center_hz - span_hz / 2
        self._image.setRect(pg.QtCore.QRectF(start_hz, 0, span_hz, _HISTORY_ROWS))
        self._plot.setXRange(start_hz, start_hz + span_hz, padding=0)
        self._plot.setYRange(0, _HISTORY_ROWS, padding=0)

    def push_row(self, power_db: np.ndarray) -> None:
        """Append one FFT power row (in dB) to the bottom of the waterfall."""
        if self._history is None or power_db.size != self._bins:
            return
        self._history[:-1] = self._history[1:]
        self._history[-1] = power_db
        self._image.setImage(self._history, autoLevels=False)
        if self._auto_levels:
            # The first row fits immediately, so the picture is right from the
            # start rather than for a fifth of a second of clipped amber.
            if self._fits_done == 0 or self._refit_countdown <= 0:
                self._refit_countdown = _AUTO_REFIT_ROWS
                self._fit_levels()
            else:
                self._refit_countdown -= 1

    # ------------------------------------------------------------------
    # Display range
    # ------------------------------------------------------------------

    def levels(self) -> tuple[float, float]:
        return self._levels

    @property
    def auto_levels(self) -> bool:
        return self._auto_levels

    def set_levels(self, low_db: float, high_db: float) -> None:
        """Set the display window explicitly."""
        if high_db <= low_db:
            raise ValueError(f"high must exceed low, got {low_db} and {high_db}")
        self._levels = (float(low_db), float(high_db))
        self._image.setLevels(self._levels)
        self.levels_changed.emit(*self._levels)

    def set_auto_levels(self, enabled: bool) -> None:
        """Turn fitting on or off. Turning it on re-fits immediately."""
        self._auto_levels = bool(enabled)
        if self._auto_levels:
            self._fits_done = 0
            self._fit_levels()
        else:
            self.set_levels(*self._levels)

    def fit_levels(self) -> None:
        """Re-fit now, whatever the automatic schedule is doing."""
        self._refit_countdown = _AUTO_REFIT_ROWS
        self._fit_levels()

    def _fit_levels(self) -> None:
        """Fit the display window to what is actually arriving.

        The reason this exists: a fixed range only works when it happens to match
        the dB scale feeding the view. When it does not, every bin sits past the
        top of the colormap and the panel becomes one flat block of the top
        colour — a saturated band looks identical to no display at all.
        """
        if self._history is None:
            return
        finite = self._history[np.isfinite(self._history)]
        if finite.size < self._bins:
            return  # nothing worth measuring yet

        low = float(np.percentile(finite, AUTO_LOW_PERCENTILE))
        high = float(np.percentile(finite, AUTO_HIGH_PERCENTILE))
        if high - low < MIN_LEVELS_SPAN_DB:
            centre = (low + high) / 2
            low = centre - MIN_LEVELS_SPAN_DB / 2
            high = centre + MIN_LEVELS_SPAN_DB / 2

        self._fits_done += 1
        if (
            abs(low - self._levels[0]) < _REFIT_EPSILON_DB
            and abs(high - self._levels[1]) < _REFIT_EPSILON_DB
        ):
            return  # stable band: no point repainting the same window
        self.set_levels(low, high)

    def clear(self) -> None:
        if self._history is not None:
            self._history.fill(np.nan)
            self._image.setImage(self._history, autoLevels=False)

    def reset_view(self) -> None:
        """Undo any mouse panning or zooming, back to the captured span."""
        if self._bins == 0:
            return
        start_hz = self._center_hz - self._span_hz / 2
        self._plot.setXRange(start_hz, start_hz + self._span_hz, padding=0)
        self._plot.setYRange(0, _HISTORY_ROWS, padding=0)

    # ------------------------------------------------------------------
    # Channel markers
    # ------------------------------------------------------------------

    def set_markers(self, markers, custom_markers=()) -> None:
        """Overlay where known mesh channels sit in the band.

        `markers` is a sequence of analytics.lora_bands.ChannelMarker.
        Each is drawn as a shaded band the width of its LoRa bandwidth, so
        you can see whether the energy in the waterfall lines up with a
        channel the mesh actually uses. `custom_markers` is the subset of
        `markers` that the user added manually (e.g. a fixed-frequency
        override that isn't derivable from any channel/preset math) — drawn
        in amber to distinguish them from computed slots.
        """
        for item in self._marker_items:
            self._plot.removeItem(item)
        self._marker_items.clear()

        custom_set = set(custom_markers)
        for m in markers:
            center = m.center_mhz * 1e6
            half = (m.bandwidth_khz * 1e3) / 2
            label_lower = m.label.lower()
            is_active = "active" in label_lower or "nominal" in label_lower
            is_custom = m in custom_set

            if is_custom:
                color = "#FFB800"
            elif is_active:
                color = "#00D4FF"
            else:
                color = "#3A4870"

            region = pg.LinearRegionItem(
                values=(center - half, center + half),
                brush=pg.mkBrush(*pg.mkColor(color).getRgb()[:3], 45 if (is_active or is_custom) else 18),
                pen=pg.mkPen(color, width=2 if (is_active or is_custom) else 1),
                movable=False,
            )
            region.setZValue(10)
            self._plot.addItem(region)
            self._marker_items.append(region)

            label = pg.TextItem(
                m.label,
                color=color if (is_active or is_custom) else "#5A6690",
                anchor=(0.5, 0),
            )
            label.setPos(center, 0)
            label.setZValue(11)
            self._plot.addItem(label)
            self._marker_items.append(label)
