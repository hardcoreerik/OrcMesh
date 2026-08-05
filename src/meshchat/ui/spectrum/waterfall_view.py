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
