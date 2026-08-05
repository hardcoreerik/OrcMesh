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

from meshchat.analytics.lora_bands import (
    MESHCORE_PLANS,
    MESHTASTIC_REGIONS,
    meshcore_markers,
    meshtastic_markers,
)
from meshchat.ui.spectrum.waterfall_view import WaterfallView

log = logging.getLogger(__name__)


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

        tb.addWidget(QLabel("Protocol:"))
        self._protocol = QComboBox()
        self._protocol.setFixedWidth(110)
        self._protocol.addItems(["Meshtastic", "MeshCore"])
        self._protocol.currentIndexChanged.connect(self._on_protocol_changed)
        tb.addWidget(self._protocol)

        tb.addWidget(QLabel("Region:"))
        self._band = QComboBox()
        self._band.setFixedWidth(150)
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

        self._marker_lbl = QLabel("")
        self._marker_lbl.setStyleSheet("color: #00D4FF; font-size: 11px;")
        tb.addWidget(self._marker_lbl)

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

        # Populated from the connected radio when available, so the markers
        # reflect the mesh you're actually on rather than a generic default.
        self._live_region: str | None = None
        self._live_preset: str | None = None
        self._live_channel_num: int = 0

        self._on_protocol_changed(0)
        self._refresh_availability()

    # ------------------------------------------------------------------
    # Band plan / markers
    # ------------------------------------------------------------------

    def set_radio_config(self, summary) -> None:
        """Take the live LoRa config from the connected radio so the spectrum
        view marks the exact slot this mesh is using."""
        if summary is None:
            self._live_region = None
            return
        self._live_region = summary.region
        self._live_preset = summary.modem_preset if summary.use_preset else None
        self._live_channel_num = summary.channel_num

        # Follow the radio: switch to Meshtastic + its region automatically
        if summary.region:
            self._protocol.setCurrentIndex(0)
            idx = self._band.findData(summary.region)
            if idx >= 0:
                self._band.setCurrentIndex(idx)
        self._apply_markers()

    def _on_protocol_changed(self, _idx: int) -> None:
        is_meshcore = self._protocol.currentText() == "MeshCore"
        self._band.blockSignals(True)
        self._band.clear()
        if is_meshcore:
            for key, plan in MESHCORE_PLANS.items():
                self._band.addItem(plan.region, userData=key)
        else:
            for key, band in MESHTASTIC_REGIONS.items():
                self._band.addItem(band.name, userData=key)
            default = self._band.findData(self._live_region or "US")
            if default >= 0:
                self._band.setCurrentIndex(default)
        self._band.blockSignals(False)
        self._on_band_changed(self._band.currentIndex())

    def _current_markers(self) -> list:
        key = self._band.currentData()
        if not key:
            return []
        if self._protocol.currentText() == "MeshCore":
            return meshcore_markers(key)
        # Mark the active slot plus a couple either side, so neighbouring
        # meshes sharing the band are visible too.
        channel_num = self._live_channel_num if key == self._live_region else 0
        return meshtastic_markers(key, self._live_preset, channel_num, include_neighbours=2)

    def _apply_markers(self) -> None:
        markers = self._current_markers()
        self._waterfall.set_markers(markers)
        if not markers:
            self._marker_lbl.setText("")
            self._marker_lbl.setToolTip("")
            return

        primary = next(
            (m for m in markers
             if "active" in m.label.lower() or "nominal" in m.label.lower()),
            markers[0],
        )
        self._marker_lbl.setText(f"Marked: {primary.label} @ {primary.center_mhz:.3f} MHz")
        if "nominal" in primary.label.lower():
            self._marker_lbl.setToolTip(
                "The radio reports channel 0, which means the firmware picks the "
                "actual slot by hashing the channel name. This marks the nominal "
                "slot 0 — the real transmissions may sit elsewhere in the band, so "
                "trust the energy in the waterfall over this marker."
            )
        else:
            self._marker_lbl.setToolTip(
                f"{primary.bandwidth_khz:.0f} kHz wide, centred on "
                f"{primary.center_mhz:.3f} MHz."
            )

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
        key = self._band.currentData()
        if not key:
            return
        if self._protocol.currentText() == "MeshCore":
            plan = MESHCORE_PLANS.get(key)
            if plan:
                self._center.setValue(plan.freq_mhz)
        else:
            band = MESHTASTIC_REGIONS.get(key)
            if band:
                # Centre on the active slot if we know it, else the band centre
                markers = self._current_markers()
                active = next((m for m in markers if "active" in m.label.lower()), None)
                self._center.setValue(active.center_mhz if active else band.center_mhz)
        self._apply_markers()

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
        # configure() rebuilds the plot geometry, so re-draw the markers over it
        self._apply_markers()
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
