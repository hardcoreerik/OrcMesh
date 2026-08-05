"""MeshChat – SpectrumPage: RTL-SDR waterfall for watching LoRa band activity.

This shows raw RF energy across the band. It does NOT decode LoRa packets —
demodulating LoRa from raw IQ is a separate, much larger problem. Use this to
see *that* activity is happening and roughly where in the band, not to read
mesh traffic (the connected radio does that).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meshchat.ui.spectrum.waterfall_view import WaterfallView

log = logging.getLogger(__name__)

# Common Meshtastic/LoRa ISM band centres, by region
_BAND_PRESETS = [
    ("US / ANZ  902–928 MHz", 915.0),
    ("EU  863–870 MHz", 868.0),
    ("CN  470–510 MHz", 490.0),
    ("433 MHz ISM", 433.5),
]


class SpectrumPage(QWidget):
    """Waterfall spectrum display driven by an RTL-SDR dongle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sdr = None
        self._running = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Toolbar ────────────────────────────────────────────────────
        toolbar = QWidget()
        toolbar.setStyleSheet("background: #0D1530; border-bottom: 1px solid #1A2448;")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(8, 6, 8, 6)
        tb.setSpacing(8)

        title = QLabel("SPECTRUM")
        title.setObjectName("monitorTitle")
        tb.addWidget(title)

        tb.addWidget(QLabel("Band:"))
        self._band = QComboBox()
        self._band.setFixedWidth(180)
        for label, mhz in _BAND_PRESETS:
            self._band.addItem(label, userData=mhz)
        self._band.currentIndexChanged.connect(self._on_band_changed)
        tb.addWidget(self._band)

        tb.addWidget(QLabel("Center (MHz):"))
        self._center = QDoubleSpinBox()
        self._center.setDecimals(3)
        self._center.setRange(24.0, 1766.0)
        self._center.setValue(915.0)
        self._center.setFixedWidth(100)
        tb.addWidget(self._center)

        tb.addWidget(QLabel("Sample rate (MS/s):"))
        self._rate = QDoubleSpinBox()
        self._rate.setDecimals(2)
        self._rate.setRange(0.25, 3.20)
        self._rate.setValue(2.40)
        self._rate.setFixedWidth(80)
        tb.addWidget(self._rate)

        self._start_btn = QPushButton("Start")
        self._start_btn.setFixedWidth(70)
        self._start_btn.clicked.connect(self._on_start_stop)
        tb.addWidget(self._start_btn)

        tb.addStretch()

        self._status = QLabel("")
        self._status.setStyleSheet("color: #5A6690; font-size: 11px;")
        tb.addWidget(self._status)

        layout.addWidget(toolbar)

        # ── Waterfall ──────────────────────────────────────────────────
        self._waterfall = WaterfallView()
        layout.addWidget(self._waterfall, 1)

        # ── Unavailable notice (shown when there's no usable SDR) ──────
        self._notice = QLabel("")
        self._notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._notice.setWordWrap(True)
        self._notice.setStyleSheet("color: #5A6690; font-size: 13px; padding: 30px;")
        self._notice.setVisible(False)
        layout.addWidget(self._notice)

        self._refresh_availability()

    # ------------------------------------------------------------------

    def _refresh_availability(self) -> None:
        from meshchat.services.sdr_source import sdr_available
        available, reason = sdr_available()
        self._start_btn.setEnabled(available)
        if available:
            self._status.setText(reason)
            self._notice.setVisible(False)
            self._waterfall.setVisible(True)
        else:
            self._status.setText("RTL-SDR unavailable")
            self._notice.setText(
                "RTL-SDR spectrum monitoring is unavailable.\n\n"
                f"{reason}\n\n"
                "This view shows raw RF energy in the band — it does not decode "
                "LoRa packets. Mesh traffic continues to come from the connected radio."
            )
            self._notice.setVisible(True)
            self._waterfall.setVisible(False)

    def _on_band_changed(self, idx: int) -> None:
        mhz = self._band.currentData()
        if mhz:
            self._center.setValue(mhz)

    def _on_start_stop(self) -> None:
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        from meshchat.services.sdr_source import SdrController

        if self._sdr is None:
            self._sdr = SdrController(self)
            self._sdr.row_ready.connect(self._waterfall.push_row)
            self._sdr.started.connect(self._on_started)
            self._sdr.stopped.connect(self._on_stopped)
            self._sdr.error.connect(self._on_error)

        center_hz = self._center.value() * 1e6
        rate_hz = self._rate.value() * 1e6
        self._sdr.start(center_hz, rate_hz, -1.0)  # -1 = automatic gain
        self._running = True
        self._start_btn.setText("Stop")
        self._status.setText("Starting…")

    def _stop(self) -> None:
        if self._sdr:
            self._sdr.stop()
        self._running = False
        self._start_btn.setText("Start")

    def _on_started(self, center_hz: float, span_hz: float, bins: int) -> None:
        self._waterfall.configure(center_hz, span_hz, bins)
        self._status.setText(
            f"Capturing — {center_hz / 1e6:.3f} MHz, {span_hz / 1e6:.2f} MS/s"
        )

    def _on_stopped(self, reason: str) -> None:
        self._running = False
        self._start_btn.setText("Start")
        self._status.setText(reason)

    def _on_error(self, message: str) -> None:
        self._running = False
        self._start_btn.setText("Start")
        self._status.setText("Error")
        self._notice.setText(message)
        self._notice.setVisible(True)
        log.error("SDR error: %s", message)

    def shutdown(self) -> None:
        if self._sdr:
            self._sdr.shutdown()
            self._sdr = None
