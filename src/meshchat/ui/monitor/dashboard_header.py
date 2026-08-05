"""MeshChat – DashboardHeader: monitor status strip."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from meshchat.controllers.meshtastic_controller import ConnectionState

log = logging.getLogger(__name__)


class DashboardHeader(QWidget):
    """Compact status strip at the top of the Monitor page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("monitorHeader")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(12)

        # Title
        title = QLabel("MESHTASTIC NETWORK MONITOR")
        title.setObjectName("monitorTitle")
        layout.addWidget(title)

        sep = QLabel("|")
        sep.setStyleSheet("color: #1A2448;")
        layout.addWidget(sep)

        # Capture state
        self._capture_lbl = QLabel("Capture: Live")
        self._capture_lbl.setObjectName("headerTelemetry")
        layout.addWidget(self._capture_lbl)

        sep2 = QLabel("|")
        sep2.setStyleSheet("color: #1A2448;")
        layout.addWidget(sep2)

        # Connection info
        self._conn_lbl = QLabel("Not connected")
        self._conn_lbl.setObjectName("headerTelemetry")
        layout.addWidget(self._conn_lbl)

        sep3 = QLabel("|")
        sep3.setStyleSheet("color: #1A2448;")
        layout.addWidget(sep3)

        # Radio LoRa config (region / modem preset / channel)
        self._radio_cfg_lbl = QLabel("")
        self._radio_cfg_lbl.setObjectName("headerTelemetry")
        self._radio_cfg_lbl.setStyleSheet("color: #00D4FF;")
        self._radio_cfg_lbl.setVisible(False)
        layout.addWidget(self._radio_cfg_lbl)

        self._sep_radio = QLabel("|")
        self._sep_radio.setStyleSheet("color: #1A2448;")
        self._sep_radio.setVisible(False)
        layout.addWidget(self._sep_radio)

        # Last updated
        self._updated_lbl = QLabel("Last updated: —")
        self._updated_lbl.setObjectName("headerTelemetry")
        layout.addWidget(self._updated_lbl)

        layout.addStretch()

        # Telemetry values (local node environment)
        self._temp_lbl = QLabel("")
        self._temp_lbl.setObjectName("headerTelemetry")
        self._temp_lbl.setToolTip("Local environment telemetry — temperature")
        self._temp_lbl.setVisible(False)
        layout.addWidget(self._temp_lbl)

        self._hum_lbl = QLabel("")
        self._hum_lbl.setObjectName("headerTelemetry")
        self._hum_lbl.setToolTip("Local environment telemetry — humidity")
        self._hum_lbl.setVisible(False)
        layout.addWidget(self._hum_lbl)

        self._pres_lbl = QLabel("")
        self._pres_lbl.setObjectName("headerTelemetry")
        self._pres_lbl.setToolTip("Local environment telemetry — barometric pressure")
        self._pres_lbl.setVisible(False)
        layout.addWidget(self._pres_lbl)

        layout.addStretch()

        # Action buttons
        self._pause_btn = QPushButton("⏸ Pause")
        self._pause_btn.setFixedWidth(80)
        self._pause_btn.clicked.connect(self._toggle_pause)
        layout.addWidget(self._pause_btn)

        self._paused = False
        self._last_updated: datetime | None = None

        # 1-second tick for relative timestamps
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_clock)
        self._tick.start(1000)

    # ------------------------------------------------------------------

    def set_connection(self, state: ConnectionState, name: str = "") -> None:
        if state == ConnectionState.CONNECTED:
            self._conn_lbl.setText(f"Connected: {name or 'Unknown'}")
        else:
            self._conn_lbl.setText("Not connected")
            self.set_radio_config(None)

    def set_radio_config(self, summary) -> None:
        """summary: LoRaConfigSummary | None"""
        if summary is None:
            self._radio_cfg_lbl.setVisible(False)
            self._sep_radio.setVisible(False)
            return

        region = summary.region or "UNSET"
        if summary.use_preset:
            preset = (summary.modem_preset or "?").replace("_", " ").title()
            radio_desc = f"{region} · {preset} · Ch {summary.channel_num}"
        else:
            radio_desc = (
                f"{region} · {summary.bandwidth_khz}kHz/SF{summary.spread_factor}/CR{summary.coding_rate} "
                f"· Ch {summary.channel_num}"
            )
        tooltip = (
            f"Region: {region}\n"
            f"Frequency plan channel: {summary.channel_num}\n"
            f"Frequency offset: {summary.frequency_offset_mhz:+.3f} MHz\n"
            f"TX power: {summary.tx_power_dbm} dBm\n"
            f"Hop limit: {summary.hop_limit}"
        )
        self._radio_cfg_lbl.setText(f"📻 {radio_desc}")
        self._radio_cfg_lbl.setToolTip(tooltip)
        self._radio_cfg_lbl.setVisible(True)
        self._sep_radio.setVisible(True)

    def mark_updated(self) -> None:
        self._last_updated = datetime.now(timezone.utc)
        self._update_clock()

    def set_env_telemetry(
        self,
        temp_c: float | None = None,
        humidity: float | None = None,
        pressure_hpa: float | None = None,
    ) -> None:
        if temp_c is not None:
            temp_f = temp_c * 9 / 5 + 32
            self._temp_lbl.setText(f"🌡 {temp_f:.1f}°F")
            self._temp_lbl.setVisible(True)
        else:
            self._temp_lbl.setVisible(False)

        if humidity is not None:
            self._hum_lbl.setText(f"💧 {humidity:.0f}%")
            self._hum_lbl.setVisible(True)
        else:
            self._hum_lbl.setVisible(False)

        if pressure_hpa is not None:
            self._pres_lbl.setText(f"⏲ {pressure_hpa:.1f} hPa")
            self._pres_lbl.setVisible(True)
        else:
            self._pres_lbl.setVisible(False)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._capture_lbl.setText("Capture: Paused")
            self._pause_btn.setText("▶ Resume")
        else:
            self._capture_lbl.setText("Capture: Live")
            self._pause_btn.setText("⏸ Pause")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _toggle_pause(self) -> None:
        self.set_paused(not self._paused)

    def _update_clock(self) -> None:
        if self._last_updated:
            ts = self._last_updated.astimezone(tz=None)
            self._updated_lbl.setText(f"Last updated: {ts.strftime('%H:%M:%S')}")
