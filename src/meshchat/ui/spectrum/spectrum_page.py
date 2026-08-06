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
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meshchat.analytics.lora_bands import (
    MESHCORE_PLANS,
    MESHTASTIC_REGIONS,
    ChannelMarker,
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
        # Text can come from a user-typed custom marker label — force plain
        # text so it can't be interpreted as rich/HTML text (QLabel
        # auto-detects rich text by content).
        self._marker_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._marker_lbl.setStyleSheet("color: #00D4FF; font-size: 11px;")
        tb.addWidget(self._marker_lbl)

        # Separate from self._status (SDR availability / capture-lifecycle
        # messages) — a band change doesn't affect either of those, so
        # writing to self._status here would silently stomp whatever
        # capture-state message was already showing.
        self._band_warning_lbl = QLabel("")
        self._band_warning_lbl.setStyleSheet("color: #FFB800; font-size: 11px;")
        tb.addWidget(self._band_warning_lbl)

        self._status = QLabel("")
        self._status.setStyleSheet("color: #5A6690; font-size: 11px;")
        tb.addWidget(self._status)

        layout.addWidget(toolbar)

        # ── Custom frequency marker toolbar ─────────────────────────────
        # For community meshes that override to a fixed frequency instead of
        # a computed channel slot (e.g. MeshOregon's 918.5 MHz override) —
        # no region/preset/channel-number math can predict those, so let the
        # user drop a marker manually.
        custom_toolbar = QWidget()
        custom_toolbar.setStyleSheet("background: #0D1530; border-bottom: 1px solid #1A2448;")
        ctb = QHBoxLayout(custom_toolbar)
        ctb.setContentsMargins(8, 6, 8, 6)
        ctb.setSpacing(8)

        ctb.addWidget(QLabel("Custom marker:"))
        self._custom_label = QLineEdit()
        self._custom_label.setPlaceholderText("Label (e.g. MeshOregon)")
        self._custom_label.setFixedWidth(150)
        ctb.addWidget(self._custom_label)

        ctb.addWidget(QLabel("MHz:"))
        self._custom_freq = QDoubleSpinBox()
        self._custom_freq.setDecimals(3)
        self._custom_freq.setRange(24.0, 1766.0)
        self._custom_freq.setValue(915.0)
        self._custom_freq.setFixedWidth(100)
        ctb.addWidget(self._custom_freq)

        ctb.addWidget(QLabel("BW (kHz):"))
        self._custom_bw = QDoubleSpinBox()
        self._custom_bw.setDecimals(0)
        self._custom_bw.setRange(1.0, 1000.0)
        self._custom_bw.setValue(125.0)
        self._custom_bw.setFixedWidth(70)
        ctb.addWidget(self._custom_bw)

        add_marker_btn = QPushButton("Add")
        add_marker_btn.setFixedWidth(60)
        add_marker_btn.clicked.connect(self._on_add_custom_marker)
        ctb.addWidget(add_marker_btn)

        clear_markers_btn = QPushButton("Clear")
        clear_markers_btn.setFixedWidth(60)
        clear_markers_btn.clicked.connect(self._on_clear_custom_markers)
        ctb.addWidget(clear_markers_btn)

        ctb.addStretch()
        layout.addWidget(custom_toolbar)

        self._custom_markers: list[ChannelMarker] = []

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

    def _base_markers(self) -> list:
        """Computed region/preset markers only — no user-added custom ones.

        Kept separate from `_current_markers()` because the "active"/
        "nominal" substring heuristics below only make sense against labels
        this code generated itself; a user-typed custom label containing
        those words must not be able to steal the recenter target or the
        status line (see _on_band_changed / _apply_markers).
        """
        key = self._band.currentData()
        if self._protocol.currentText() == "MeshCore":
            return meshcore_markers(key) if key else []
        # Mark the active slot plus a couple either side, so neighbouring
        # meshes sharing the band are visible too.
        channel_num = self._live_channel_num if key == self._live_region else 0
        return meshtastic_markers(key, self._live_preset, channel_num, include_neighbours=2) if key else []

    def _current_markers(self) -> list:
        return self._base_markers() + self._custom_markers

    def _on_add_custom_marker(self) -> None:
        label = self._custom_label.text().strip() or f"{self._custom_freq.value():.3f} MHz"
        self._custom_markers.append(
            ChannelMarker(label, self._custom_freq.value(), self._custom_bw.value())
        )
        self._apply_markers()

    def _on_clear_custom_markers(self) -> None:
        self._custom_markers.clear()
        self._apply_markers()

    def _apply_markers(self) -> None:
        markers = self._current_markers()
        self._waterfall.set_markers(markers, custom_markers=self._custom_markers)
        if not markers:
            self._marker_lbl.setText("")
            self._marker_lbl.setToolTip("")
            return

        base_markers = self._base_markers()
        primary = next(
            (m for m in base_markers
             if "active" in m.label.lower() or "nominal" in m.label.lower()),
            base_markers[0] if base_markers else markers[0],
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
        target_mhz: float | None = None
        if self._protocol.currentText() == "MeshCore":
            plan = MESHCORE_PLANS.get(key)
            if plan:
                target_mhz = plan.freq_mhz
        else:
            band = MESHTASTIC_REGIONS.get(key)
            if band:
                # Centre on the active slot if we know it, else the band centre.
                # Base markers only — a user custom label containing "active"
                # must not steal the recenter target.
                markers = self._base_markers()
                active = next((m for m in markers if "active" in m.label.lower()), None)
                target_mhz = active.center_mhz if active else band.center_mhz

        if target_mhz is not None:
            # QDoubleSpinBox.setValue() silently clamps to the configured
            # range with no error — e.g. selecting the 2.4 GHz Meshtastic
            # region (center ~2441.75 MHz) against the 24-1766 MHz range
            # this RTL-SDR tuner chip actually supports would otherwise
            # display 1766 MHz with no indication anything went wrong.
            if not (self._center.minimum() <= target_mhz <= self._center.maximum()):
                self._band_warning_lbl.setText(
                    f"⚠ {target_mhz:.1f} MHz is outside this SDR's tunable range "
                    f"({self._center.minimum():.0f}-{self._center.maximum():.0f} MHz) — "
                    "showing the closest available frequency instead."
                )
            else:
                self._band_warning_lbl.setText("")
            self._center.setValue(target_mhz)
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
