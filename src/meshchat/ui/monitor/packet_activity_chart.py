"""MeshChat – PacketActivityChart: live pyqtgraph time-series chart."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

import pyqtgraph as pg
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

log = logging.getLogger(__name__)

# pyqtgraph dark theme
pg.setConfigOption("background", "#070D1F")
pg.setConfigOption("foreground", "#7A8FBF")

_SERIES_COLORS = {
    "Text":      "#00D4FF",
    "Position":  "#00FF88",
    "NodeInfo":  "#8B5CF6",
    "Telemetry": "#FFB800",
    "Other":     "#5A6690",
}

_PORTNUM_SERIES = {
    1:  "Text",
    3:  "Position",
    4:  "NodeInfo",
    67: "Telemetry",
}


class PacketActivityChart(QWidget):
    """Live bar/line chart of packet activity per time bucket."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bucket_s = 15          # 15-second buckets (1 hour view)
        self._window_s = 3600        # 1 hour default
        self._max_buckets = self._window_s // self._bucket_s

        # Data: series_name → deque of (bucket_ts, count)
        self._data: dict[str, deque] = {
            name: deque(maxlen=self._max_buckets)
            for name in _SERIES_COLORS
        }
        self._current_bucket: int = 0
        self._bucket_counts: dict[str, int] = defaultdict(int)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Toolbar
        toolbar = QHBoxLayout()
        lbl = QLabel("PACKET ACTIVITY")
        lbl.setObjectName("sectionLabel")
        toolbar.addWidget(lbl)
        toolbar.addStretch()

        self._live_btn = QPushButton("Live ●")
        self._live_btn.setFixedWidth(65)
        self._live_btn.setCheckable(True)
        self._live_btn.setChecked(True)
        self._live_btn.setStyleSheet("font-size: 10px; padding: 2px 6px;")
        toolbar.addWidget(self._live_btn)

        layout.addLayout(toolbar)

        # Chart
        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setObjectName("packetChart")
        self._plot_widget.setLabel("left", "Packets / bucket")
        self._plot_widget.setLabel("bottom", "Time (s ago)")
        self._plot_widget.showGrid(x=False, y=True, alpha=0.2)
        self._plot_widget.getAxis("left").setStyle(tickFont=pg.Qt.QtGui.QFont("Consolas", 8))
        self._plot_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Curve per series
        self._curves: dict[str, pg.PlotDataItem] = {}
        for name, color in _SERIES_COLORS.items():
            curve = self._plot_widget.plot(
                pen=pg.mkPen(color=color, width=1.5),
                fillLevel=0,
                fillBrush=pg.mkBrush(color + "22"),
                name=name,
            )
            self._curves[name] = curve

        layout.addWidget(self._plot_widget, 1)

        # Legend (manual)
        legend_row = QHBoxLayout()
        legend_row.addStretch()
        for name, color in _SERIES_COLORS.items():
            dot = QLabel(f"● {name}")
            dot.setStyleSheet(f"color: {color}; font-size: 10px;")
            legend_row.addWidget(dot)
        layout.addLayout(legend_row)

        # Refresh timer (4 Hz)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_plot)
        self._refresh_timer.start(250)

        # Bucket flush timer (per bucket)
        self._bucket_timer = QTimer(self)
        self._bucket_timer.timeout.connect(self._flush_bucket)
        self._bucket_timer.start(self._bucket_s * 1000)

    # ------------------------------------------------------------------

    def record_packet(self, portnum: int | None) -> None:
        """Call this when a packet arrives to update counts."""
        series = _PORTNUM_SERIES.get(portnum, "Other")
        self._bucket_counts[series] += 1

    def _flush_bucket(self) -> None:
        """Move current bucket counts into history."""
        ts = int(time.time()) // self._bucket_s * self._bucket_s
        for name in _SERIES_COLORS:
            self._data[name].append((ts, self._bucket_counts.get(name, 0)))
        self._bucket_counts.clear()

    def _refresh_plot(self) -> None:
        if not self._live_btn.isChecked():
            return
        now = time.time()
        for name, dq in self._data.items():
            if not dq:
                self._curves[name].setData([], [])
                continue
            xs = []
            ys = []
            for ts, count in dq:
                xs.append(-(now - ts))
                ys.append(count)
            if xs:
                self._curves[name].setData(xs, ys)
