"""MeshChat – NodeInspector: node detail panel."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)


def _age(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    delta = (datetime.now(timezone.utc) - ts).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    return f"{int(delta / 3600)}h ago"


class _Section(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(3)

        hdr = QLabel(title.upper())
        hdr.setObjectName("sectionLabel")
        layout.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #1A2448;")
        layout.addWidget(sep)

        self._body = QVBoxLayout()
        self._body.setSpacing(2)
        layout.addLayout(self._body)

    def add_row(self, key: str, value: str, tooltip: str = "") -> QLabel:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(4, 0, 4, 0)
        row_layout.setSpacing(8)

        key_lbl = QLabel(key)
        key_lbl.setStyleSheet("color: #5A6690; font-size: 11px; min-width: 120px;")
        key_lbl.setFixedWidth(130)
        row_layout.addWidget(key_lbl)

        val_lbl = QLabel(value or "—")
        val_lbl.setStyleSheet("color: #C8D8FF; font-size: 11px; font-family: Consolas;")
        val_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        val_lbl.setWordWrap(True)
        if tooltip:
            val_lbl.setToolTip(tooltip)
        row_layout.addWidget(val_lbl, 1)

        self._body.addWidget(row)
        return val_lbl


class NodeInspector(QWidget):
    """Detailed node inspector panel."""

    show_on_map = Signal(int)           # node_num
    filter_packet_log = Signal(int)     # node_num

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        self._node_num: int | None = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Header
        self._header_lbl = QLabel("No node selected")
        self._header_lbl.setStyleSheet(
            "color: #00D4FF; font-size: 14px; font-weight: 700; padding: 10px;"
        )
        main_layout.addWidget(self._header_lbl)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(8, 0, 8, 6)
        btn_row.setSpacing(4)

        self._map_btn = QPushButton("Show on Map")
        self._map_btn.setFixedHeight(24)
        self._map_btn.setStyleSheet("font-size: 10px; padding: 2px 8px;")
        self._map_btn.clicked.connect(self._on_map)
        self._map_btn.setEnabled(False)
        btn_row.addWidget(self._map_btn)

        self._log_btn = QPushButton("Filter Log")
        self._log_btn.setFixedHeight(24)
        self._log_btn.setStyleSheet("font-size: 10px; padding: 2px 8px;")
        self._log_btn.clicked.connect(self._on_filter_log)
        self._log_btn.setEnabled(False)
        btn_row.addWidget(self._log_btn)

        btn_row.addStretch()
        main_layout.addLayout(btn_row)

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 0, 8, 8)
        content_layout.setSpacing(0)

        # Identity section
        id_sec = _Section("Identity")
        self._long_name = id_sec.add_row("Long Name", "—")
        self._short_name = id_sec.add_row("Short Name", "—")
        self._node_id = id_sec.add_row("Node ID", "—")
        self._node_num_lbl = id_sec.add_row("Node Number", "—")
        self._role = id_sec.add_row("Role", "—")
        self._hw = id_sec.add_row("Hardware", "—")
        content_layout.addWidget(id_sec)

        # Activity section
        act_sec = _Section("Activity")
        self._first_seen = act_sec.add_row("First Seen", "—")
        self._last_heard = act_sec.add_row("Last Heard", "—")
        self._pkt_count = act_sec.add_row("Packets Observed", "0")
        self._text_count = act_sec.add_row("Text Messages", "0")
        self._rf_count = act_sec.add_row("RF Packets", "0")
        self._mqtt_count = act_sec.add_row("MQTT Packets", "0")
        content_layout.addWidget(act_sec)

        # Signal section
        sig_sec = _Section("Signal (Last-hop RF)")
        self._snr = sig_sec.add_row(
            "SNR",
            "—",
            "SNR at the connected radio's final RF hop — not necessarily the original sender."
        )
        self._rssi = sig_sec.add_row(
            "RSSI",
            "—",
            "RSSI at the connected radio's final RF hop."
        )
        self._noise = sig_sec.add_row(
            "Noise",
            "N/A",
            "No explicit noise-floor measurement is available from the current source."
        )
        content_layout.addWidget(sig_sec)

        # Hops section
        hop_sec = _Section("Hops")
        self._hops_used = hop_sec.add_row("Last Hops Used", "—")
        self._hops_left = hop_sec.add_row("Last Hops Left", "—")
        self._hop_start = hop_sec.add_row("Hop Start", "—")
        content_layout.addWidget(hop_sec)

        # Telemetry section
        tel_sec = _Section("Telemetry")
        self._battery = tel_sec.add_row("Battery", "—")
        self._voltage = tel_sec.add_row("Voltage", "—")
        self._chan_util = tel_sec.add_row("Channel Util.", "—")
        self._air_util = tel_sec.add_row("Air Util. TX", "—")
        self._temp = tel_sec.add_row("Temperature", "—")
        self._humidity = tel_sec.add_row("Humidity", "—")
        self._pressure = tel_sec.add_row("Pressure", "—")
        self._tel_age = tel_sec.add_row("Last Reading", "—")
        content_layout.addWidget(tel_sec)

        content_layout.addStretch()
        scroll.setWidget(content)
        main_layout.addWidget(scroll, 1)

    # ------------------------------------------------------------------

    def show_node(self, snapshot) -> None:
        """Populate from a NodeSnapshot object."""
        self._node_num = snapshot.node_num
        self._map_btn.setEnabled(True)
        self._log_btn.setEnabled(True)

        self._header_lbl.setText(snapshot.display_name)
        self._long_name.setText(snapshot.long_name or "—")
        self._short_name.setText(snapshot.short_name or "—")
        self._node_id.setText(snapshot.node_id or f"!{snapshot.node_num:08x}")
        self._node_num_lbl.setText(str(snapshot.node_num))
        self._role.setText(snapshot.role or "—")
        self._hw.setText(snapshot.hw_model or "—")

        self._first_seen.setText(_age(snapshot.first_seen))
        self._last_heard.setText(_age(snapshot.last_heard))
        self._pkt_count.setText(str(snapshot.packet_count))
        self._text_count.setText(str(snapshot.text_count))
        self._rf_count.setText(str(snapshot.rf_count))
        self._mqtt_count.setText(str(snapshot.via_mqtt_count))

        self._snr.setText(f"{snapshot.last_snr:.1f} dB" if snapshot.last_snr is not None else "—")
        self._rssi.setText(f"{snapshot.last_rssi} dBm" if snapshot.last_rssi is not None else "—")
        self._noise.setText("N/A")

        self._hops_used.setText(str(snapshot.last_hops_used) if snapshot.last_hops_used is not None else "—")
        if snapshot.last_hops_used is not None and snapshot.last_hop_start is not None:
            left = snapshot.last_hop_start - snapshot.last_hops_used
            self._hops_left.setText(str(left))
        else:
            self._hops_left.setText("—")
        self._hop_start.setText(str(snapshot.last_hop_start) if snapshot.last_hop_start is not None else "—")

        battery = snapshot.battery_level
        self._battery.setText(
            "Plugged in" if battery is not None and battery > 100
            else f"{battery:.0f}%" if battery is not None else "—"
        )
        self._voltage.setText(f"{snapshot.voltage:.2f} V" if snapshot.voltage is not None else "—")
        self._chan_util.setText(
            f"{snapshot.channel_utilization:.1f}%" if snapshot.channel_utilization is not None else "—"
        )
        self._air_util.setText(
            f"{snapshot.air_util_tx:.1f}%" if snapshot.air_util_tx is not None else "—"
        )
        self._temp.setText(
            f"{snapshot.temperature_c * 9 / 5 + 32:.1f}°F" if snapshot.temperature_c is not None else "—"
        )
        self._humidity.setText(
            f"{snapshot.relative_humidity:.0f}%" if snapshot.relative_humidity is not None else "—"
        )
        self._pressure.setText(
            f"{snapshot.barometric_pressure_hpa:.1f} hPa" if snapshot.barometric_pressure_hpa is not None else "—"
        )
        self._tel_age.setText(_age(snapshot.last_telemetry_at))

    def clear(self) -> None:
        self._node_num = None
        self._header_lbl.setText("No node selected")
        self._map_btn.setEnabled(False)
        self._log_btn.setEnabled(False)

    # ------------------------------------------------------------------

    def _on_map(self) -> None:
        if self._node_num is not None:
            self.show_on_map.emit(self._node_num)

    def _on_filter_log(self) -> None:
        if self._node_num is not None:
            self.filter_packet_log.emit(self._node_num)
