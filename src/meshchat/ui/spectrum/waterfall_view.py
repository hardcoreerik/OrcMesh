"""MeshChat – WaterfallView: RTL-SDR spectrum waterfall display.

The rendering side is hardware-independent: feed it FFT power rows via
`push_row()` and it scrolls a waterfall. The SDR capture backend
(`sdr_source.py`) supplies those rows when a dongle is present.
"""
from __future__ import annotations

import logging

import numpy as np
import pyqtgraph as pg
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


def _build_colormap() -> pg.ColorMap:
    positions = np.array([s[0] for s in _COLORMAP_STOPS])
    colors = np.array([s[1] for s in _COLORMAP_STOPS], dtype=np.ubyte)
    return pg.ColorMap(positions, colors)


class WaterfallView(QWidget):
    """Scrolling spectrum waterfall. Newest row appears at the bottom."""

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
        self._image.setColorMap(_build_colormap())
        self._plot.addItem(self._image)

        self._bins = 0
        self._history: np.ndarray | None = None
        self._center_hz = 0.0
        self._span_hz = 0.0
        self._marker_items: list = []

    # ------------------------------------------------------------------

    def configure(self, center_hz: float, span_hz: float, bins: int) -> None:
        """Reset the waterfall for a new capture geometry."""
        self._center_hz = center_hz
        self._span_hz = span_hz
        self._bins = bins
        self._history = np.zeros((_HISTORY_ROWS, bins), dtype=np.float32)
        self._image.setImage(self._history, autoLevels=False)
        self._image.setLevels((-110.0, -20.0))  # dBFS-ish display range

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

    def clear(self) -> None:
        if self._history is not None:
            self._history.fill(-120.0)
            self._image.setImage(self._history, autoLevels=False)

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
