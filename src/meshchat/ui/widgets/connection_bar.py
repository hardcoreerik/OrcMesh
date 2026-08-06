"""MeshChat – ConnectionBar: transport selector, BLE/TCP inputs, state indicator."""
from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from meshchat.controllers.meshtastic_controller import ConnectionState
from meshchat.models.connection_profile import ConnectionProfile

log = logging.getLogger(__name__)

_STATE_SYMBOLS = {
    ConnectionState.DISCONNECTED:  ("●", "disconnected"),
    ConnectionState.SCANNING:      ("◉", "scanning"),
    ConnectionState.CONNECTING:    ("◌", "connecting"),
    ConnectionState.SYNCING:       ("◌", "connecting"),
    ConnectionState.CONNECTED:     ("●", "connected"),
    ConnectionState.RECONNECTING:  ("◌", "connecting"),
    ConnectionState.DISCONNECTING: ("◌", "connecting"),
    ConnectionState.ERROR:         ("✕", "error"),
}

_STATE_LABELS = {
    ConnectionState.DISCONNECTED:  "Disconnected",
    ConnectionState.SCANNING:      "Scanning…",
    ConnectionState.CONNECTING:    "Connecting…",
    ConnectionState.SYNCING:       "Downloading config…",
    ConnectionState.CONNECTED:     "Connected",
    ConnectionState.RECONNECTING:  "Reconnecting…",
    ConnectionState.DISCONNECTING: "Disconnecting…",
    ConnectionState.ERROR:         "Connection failed",
}


class ConnectionBar(QWidget):
    """Top bar: transport controls + connection state indicator."""

    # Signals to controller
    scan_requested = Signal()
    connect_ble_requested = Signal(str)              # address
    connect_tcp_requested = Signal(str, int)         # host, port
    connect_serial_requested = Signal(str)           # COM port
    list_serial_ports_requested = Signal()
    disconnect_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("connectionBar")
        self._state = ConnectionState.DISCONNECTED
        self._ble_devices: list = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(6)

        # Transport selector
        self._transport = QComboBox()
        self._transport.addItems(["Bluetooth", "Wi-Fi / TCP", "USB / Serial"])
        self._transport.setFixedWidth(110)
        self._transport.currentIndexChanged.connect(self._on_transport_changed)
        layout.addWidget(self._transport)

        # BLE device combo (Bluetooth mode)
        self._ble_combo = QComboBox()
        self._ble_combo.setFixedWidth(200)
        self._ble_combo.setPlaceholderText("Select device…")
        layout.addWidget(self._ble_combo)

        # BLE scan button
        self._scan_btn = QPushButton("Scan")
        self._scan_btn.setFixedWidth(55)
        self._scan_btn.clicked.connect(self.scan_requested)
        layout.addWidget(self._scan_btn)

        # TCP host/port (TCP mode, hidden initially)
        self._tcp_host = QLineEdit()
        self._tcp_host.setPlaceholderText("192.168.1.50  or  meshtastic.local")
        self._tcp_host.setFixedWidth(220)
        self._tcp_host.setVisible(False)
        layout.addWidget(self._tcp_host)

        self._tcp_port = QSpinBox()
        self._tcp_port.setRange(1, 65535)
        self._tcp_port.setValue(4403)
        self._tcp_port.setFixedWidth(70)
        self._tcp_port.setVisible(False)
        layout.addWidget(self._tcp_port)

        # Serial (USB) port combo (Serial mode, hidden initially)
        self._serial_combo = QComboBox()
        self._serial_combo.setFixedWidth(200)
        self._serial_combo.setPlaceholderText("Select COM port…")
        self._serial_combo.setVisible(False)
        layout.addWidget(self._serial_combo)

        self._serial_refresh_btn = QPushButton("Refresh")
        self._serial_refresh_btn.setFixedWidth(65)
        self._serial_refresh_btn.setVisible(False)
        self._serial_refresh_btn.clicked.connect(self.list_serial_ports_requested)
        layout.addWidget(self._serial_refresh_btn)

        # Connect / Disconnect buttons
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setFixedWidth(75)
        self._connect_btn.clicked.connect(self._on_connect)
        layout.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setObjectName("dangerBtn")
        self._disconnect_btn.setFixedWidth(90)
        self._disconnect_btn.clicked.connect(self.disconnect_requested)
        self._disconnect_btn.setEnabled(False)
        layout.addWidget(self._disconnect_btn)

        layout.addStretch()

        # Status indicator
        self._status_dot = QLabel("●")
        self._status_dot.setObjectName("statusDot")
        self._status_dot.setProperty("state", "disconnected")
        self._status_dot.setFixedWidth(14)
        layout.addWidget(self._status_dot)

        self._status_label = QLabel("Disconnected")
        self._status_label.setStyleSheet("color: #5A6690; font-size: 11px;")
        layout.addWidget(self._status_label)

        self._device_label = QLabel("")
        self._device_label.setStyleSheet("color: #00D4FF; font-size: 11px; font-weight: 600;")
        layout.addWidget(self._device_label)

        self._update_controls()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_ble_devices(self, devices: list) -> None:
        """Populate BLE dropdown after a scan."""
        self._ble_devices = devices
        self._ble_combo.clear()
        for d in devices:
            self._ble_combo.addItem(f"{d.name}  ({d.address})", userData=d.address)
        if devices:
            self._ble_combo.setCurrentIndex(0)

    def set_serial_ports(self, ports: list) -> None:
        """Populate serial-port dropdown after enumeration."""
        self._serial_combo.clear()
        for p in ports:
            self._serial_combo.addItem(f"{p.device}  ({p.description})", userData=p.device)
        if ports:
            self._serial_combo.setCurrentIndex(0)

    def set_state(self, state: ConnectionState, detail: str = "") -> None:
        self._state = state
        sym, prop = _STATE_SYMBOLS.get(state, ("●", "disconnected"))
        label = _STATE_LABELS.get(state, state.value)

        self._status_dot.setText(sym)
        self._status_dot.setProperty("state", prop)
        # Force style refresh
        self._status_dot.style().unpolish(self._status_dot)
        self._status_dot.style().polish(self._status_dot)

        self._status_label.setText(label)
        self._update_controls()

    def set_device_name(self, name: str) -> None:
        self._device_label.setText(f"  {name}" if name else "")

    def restore_profile(self, profile: ConnectionProfile) -> None:
        """Pre-fill connection fields from a saved profile.

        Called once at startup after loading the last-used profile from
        app_settings. Only fills fields that the profile actually has — a
        partial profile (e.g. transport only) is fine.
        """
        transport_idx = {"ble": 0, "tcp": 1, "serial": 2}.get(profile.transport)
        if transport_idx is not None:
            self._transport.setCurrentIndex(transport_idx)
            self._on_transport_changed(transport_idx)
        if profile.tcp_host:
            self._tcp_host.setText(profile.tcp_host)
        if profile.tcp_port and profile.tcp_port != 4403:
            self._tcp_port.setValue(profile.tcp_port)
        if profile.serial_port:
            idx = self._serial_combo.findData(profile.serial_port)
            if idx < 0:
                self._serial_combo.addItem(profile.serial_port, userData=profile.serial_port)
                idx = self._serial_combo.count() - 1
            self._serial_combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _on_transport_changed(self, idx: int) -> None:
        is_ble = (idx == 0)
        is_tcp = (idx == 1)
        is_serial = (idx == 2)
        self._ble_combo.setVisible(is_ble)
        self._scan_btn.setVisible(is_ble)
        self._tcp_host.setVisible(is_tcp)
        self._tcp_port.setVisible(is_tcp)
        self._serial_combo.setVisible(is_serial)
        self._serial_refresh_btn.setVisible(is_serial)
        if is_serial and self._serial_combo.count() == 0:
            self.list_serial_ports_requested.emit()

    def _on_connect(self) -> None:
        idx = self._transport.currentIndex()
        if idx == 0:
            # Bluetooth
            addr = self._ble_combo.currentData()
            if addr:
                self.connect_ble_requested.emit(addr)
        elif idx == 1:
            # TCP
            host = self._tcp_host.text().strip()
            port = self._tcp_port.value()
            if host:
                self.connect_tcp_requested.emit(host, port)
        else:
            # Serial / USB
            port_name = self._serial_combo.currentData()
            if port_name:
                self.connect_serial_requested.emit(port_name)

    def _update_controls(self) -> None:
        s = self._state
        busy = s in (
            ConnectionState.SCANNING,
            ConnectionState.CONNECTING,
            ConnectionState.SYNCING,
            ConnectionState.RECONNECTING,
            ConnectionState.DISCONNECTING,
        )
        connected = s == ConnectionState.CONNECTED

        self._transport.setEnabled(not busy and not connected)
        self._ble_combo.setEnabled(not busy and not connected)
        self._scan_btn.setEnabled(not busy and not connected)
        self._tcp_host.setEnabled(not busy and not connected)
        self._tcp_port.setEnabled(not busy and not connected)
        self._serial_combo.setEnabled(not busy and not connected)
        self._serial_refresh_btn.setEnabled(not busy and not connected)
        self._connect_btn.setEnabled(not busy and not connected)
        self._disconnect_btn.setEnabled(connected or busy)

        if not connected:
            self._device_label.setText("")
