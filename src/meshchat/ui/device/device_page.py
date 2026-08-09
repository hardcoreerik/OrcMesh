"""Connected Meshtastic radio configuration and maintenance page."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class DevicePage(QWidget):
    save_section_requested = Signal(str, object)
    owner_requested = Signal(str, str)
    channel_requested = Signal(object)
    reboot_requested = Signal(int)
    shutdown_requested = Signal(int)
    reset_nodedb_requested = Signal()
    factory_reset_requested = Signal(bool)
    fixed_position_requested = Signal(float, float, int)
    remove_fixed_position_requested = Signal()
    refresh_requested = Signal()
    firmware_discover_requested = Signal(str, bool)
    firmware_prepare_requested = Signal(object, str, str)
    firmware_flash_requested = Signal(object, bool, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._snapshot = None
        self._section_widgets: dict[str, dict[str, tuple[Any, Any]]] = {}
        self._channel_by_index = {}
        self._firmware_release = None
        self._firmware_bundle = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("USB Device Controls")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        header.addWidget(title)
        header.addStretch()
        self._refresh = QPushButton("Refresh")
        self._refresh.clicked.connect(self.refresh_requested)
        header.addWidget(self._refresh)
        root.addLayout(header)

        self._summary = QLabel("Connect a Meshtastic radio over USB / Serial to manage it.")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)
        self._build_overview_tab()
        self._build_settings_tab()
        self._build_channels_tab()
        self._build_firmware_tab()

        self.set_connected(False)

    def _build_overview_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        identity = QGroupBox("Identity")
        form = QFormLayout(identity)
        self._long_name = QLineEdit()
        self._short_name = QLineEdit()
        self._short_name.setMaxLength(4)
        form.addRow("Long name", self._long_name)
        form.addRow("Short name", self._short_name)
        save_owner = QPushButton("Save Identity")
        save_owner.clicked.connect(
            lambda: self.owner_requested.emit(
                self._long_name.text().strip(), self._short_name.text().strip()
            )
        )
        form.addRow("", save_owner)
        layout.addWidget(identity)

        position = QGroupBox("Fixed Position")
        pos_form = QFormLayout(position)
        self._latitude = QLineEdit()
        self._longitude = QLineEdit()
        self._altitude = QSpinBox()
        self._altitude.setRange(-1000, 100000)
        pos_form.addRow("Latitude", self._latitude)
        pos_form.addRow("Longitude", self._longitude)
        pos_form.addRow("Altitude (m)", self._altitude)
        pos_buttons = QHBoxLayout()
        set_pos = QPushButton("Set Fixed Position")
        set_pos.clicked.connect(self._set_fixed_position)
        remove_pos = QPushButton("Use GPS / Remove Fixed")
        remove_pos.clicked.connect(self.remove_fixed_position_requested)
        pos_buttons.addWidget(set_pos)
        pos_buttons.addWidget(remove_pos)
        pos_form.addRow("", pos_buttons)
        layout.addWidget(position)

        maintenance = QGroupBox("Maintenance")
        buttons = QHBoxLayout(maintenance)
        for label, handler in (
            ("Reboot", self._confirm_reboot),
            ("Shutdown", self._confirm_shutdown),
            ("Reset NodeDB", self._confirm_nodedb_reset),
            ("Factory Reset", self._confirm_factory_reset),
            ("Full Factory Reset", lambda: self._confirm_factory_reset(full=True)),
        ):
            button = QPushButton(label)
            if "Reset" in label:
                button.setObjectName("dangerBtn")
            button.clicked.connect(handler)
            buttons.addWidget(button)
        layout.addWidget(maintenance)
        layout.addStretch()
        self._tabs.addTab(page, "Overview")

    def _build_settings_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Section"))
        self._section_selector = QComboBox()
        self._section_selector.currentIndexChanged.connect(self._on_section_changed)
        selector_row.addWidget(self._section_selector, 1)
        self._save_section = QPushButton("Save Section")
        self._save_section.clicked.connect(self._save_current_section)
        selector_row.addWidget(self._save_section)
        layout.addLayout(selector_row)
        self._section_stack = QStackedWidget()
        layout.addWidget(self._section_stack, 1)
        self._tabs.addTab(page, "Configuration")

    def _build_channels_tab(self) -> None:
        page = QWidget()
        layout = QFormLayout(page)
        self._channel_selector = QComboBox()
        self._channel_selector.currentIndexChanged.connect(self._load_channel)
        self._channel_name = QLineEdit()
        self._channel_role = QComboBox()
        from meshtastic.protobuf import channel_pb2
        for value in channel_pb2.Channel.Role.values():
            self._channel_role.addItem(channel_pb2.Channel.Role.Name(value), value)
        self._channel_uplink = QCheckBox()
        self._channel_downlink = QCheckBox()
        self._position_precision = QSpinBox()
        self._position_precision.setRange(0, 32)
        self._channel_psk = QLineEdit()
        self._channel_psk.setEchoMode(QLineEdit.EchoMode.Password)
        self._channel_psk.setPlaceholderText("Unchanged; enter default, random, none, or base64 PSK")
        save = QPushButton("Save Channel")
        save.clicked.connect(self._save_channel)
        layout.addRow("Channel", self._channel_selector)
        layout.addRow("Name", self._channel_name)
        layout.addRow("Role", self._channel_role)
        layout.addRow("Uplink", self._channel_uplink)
        layout.addRow("Downlink", self._channel_downlink)
        layout.addRow("Position precision", self._position_precision)
        layout.addRow("Replacement PSK", self._channel_psk)
        layout.addRow("", save)
        self._tabs.addTab(page, "Channels")

    def _build_firmware_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        warning = QLabel(
            "Firmware is downloaded only from official meshtastic/firmware releases. "
            "OrcMesh verifies the release SHA-256, target metadata, hardware model, "
            "and every extracted firmware image before enabling Flash."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        row = QHBoxLayout()
        self._release_channel = QComboBox()
        self._release_channel.addItem("Stable", False)
        self._release_channel.addItem("Include prerelease", True)
        self._check_firmware = QPushButton("Check Official Release")
        self._check_firmware.clicked.connect(self._discover_firmware)
        self._download_firmware = QPushButton("Download & Verify")
        self._download_firmware.setEnabled(False)
        self._download_firmware.clicked.connect(self._prepare_firmware)
        row.addWidget(self._release_channel)
        row.addWidget(self._check_firmware)
        row.addWidget(self._download_firmware)
        row.addStretch()
        layout.addLayout(row)
        self._firmware_status = QLabel("No release checked.")
        self._firmware_status.setWordWrap(True)
        layout.addWidget(self._firmware_status)
        self._firmware_progress = QProgressBar()
        self._firmware_progress.setRange(0, 1000)
        self._firmware_progress.setValue(0)
        layout.addWidget(self._firmware_progress)
        flash_row = QHBoxLayout()
        self._flash_update = QPushButton("Flash Update (Preserve Settings)")
        self._flash_update.setEnabled(False)
        self._flash_update.clicked.connect(lambda: self._confirm_flash(False))
        self._flash_full = QPushButton("Full Erase & Install")
        self._flash_full.setObjectName("dangerBtn")
        self._flash_full.setEnabled(False)
        self._flash_full.clicked.connect(lambda: self._confirm_flash(True))
        flash_row.addWidget(self._flash_update)
        flash_row.addWidget(self._flash_full)
        flash_row.addStretch()
        layout.addLayout(flash_row)
        self._firmware_log = QTextEdit()
        self._firmware_log.setReadOnly(True)
        self._firmware_log.setPlaceholderText("Firmware progress will appear here. Secrets are never logged.")
        layout.addWidget(self._firmware_log, 1)
        self._tabs.addTab(page, "Firmware")

    def set_connected(self, connected: bool) -> None:
        self._tabs.setEnabled(connected)
        self._refresh.setEnabled(connected)
        if not connected:
            self._snapshot = None
            self._summary.setText("Connect a Meshtastic radio over USB / Serial to manage it.")
            self._flash_update.setEnabled(False)
            self._flash_full.setEnabled(False)

    def set_snapshot(self, snapshot) -> None:
        self._snapshot = snapshot
        self.set_connected(snapshot.serial_port is not None)
        transport = snapshot.serial_port or "non-USB connection"
        self._summary.setText(
            f"{snapshot.long_name or snapshot.node_id or 'Radio'} · {snapshot.hw_model} · "
            f"Firmware {snapshot.firmware_version} · {snapshot.pio_env} · {transport}"
        )
        self._long_name.setText(snapshot.long_name)
        self._short_name.setText(snapshot.short_name)
        self._rebuild_sections(snapshot.sections)
        self._rebuild_channels(snapshot.channels)
        self._firmware_release = None
        self._firmware_bundle = None
        self._download_firmware.setEnabled(False)
        self._flash_update.setEnabled(False)
        self._flash_full.setEnabled(False)

    def show_operation(self, detail: str) -> None:
        self._summary.setText(detail)

    def set_firmware_release(self, release) -> None:
        self._firmware_release = release
        channel = "prerelease" if release.prerelease else "stable"
        self._firmware_status.setText(
            f"Meshtastic {release.version} ({channel}) · {release.asset_name} · "
            f"{release.asset_size / 1024 / 1024:.1f} MB"
        )
        self._download_firmware.setEnabled(True)

    def set_firmware_bundle(self, bundle) -> None:
        self._firmware_bundle = bundle
        self._firmware_status.setText(
            f"Verified Meshtastic {bundle.release.version} for {bundle.hw_model} "
            f"({bundle.pio_env}). Ready to flash."
        )
        self._flash_update.setEnabled(True)
        self._flash_full.setEnabled(True)

    def set_firmware_progress(self, done: int, total: int) -> None:
        self._firmware_progress.setValue(0 if total <= 0 else min(1000, int(done * 1000 / total)))

    def append_firmware_log(self, line: str) -> None:
        self._firmware_log.append(line)

    def firmware_completed(self, operation: str, success: bool, detail: str) -> None:
        self._firmware_status.setText(detail)
        if operation == "discover":
            self._check_firmware.setEnabled(True)
        elif operation == "prepare" and not success:
            self._download_firmware.setEnabled(self._firmware_release is not None)
        if operation == "flash":
            self._flash_update.setEnabled(success is False and self._firmware_bundle is not None)
            self._flash_full.setEnabled(success is False and self._firmware_bundle is not None)
        if not success:
            self._firmware_log.append("ERROR: " + detail)

    def _discover_firmware(self) -> None:
        if self._snapshot is None or not self._snapshot.serial_port:
            return
        self._check_firmware.setEnabled(False)
        self._firmware_status.setText("Checking official Meshtastic releases…")
        self.firmware_discover_requested.emit(
            self._snapshot.pio_env, bool(self._release_channel.currentData())
        )

    def _prepare_firmware(self) -> None:
        if self._snapshot is None or self._firmware_release is None:
            return
        self._download_firmware.setEnabled(False)
        self._firmware_status.setText("Downloading and verifying firmware…")
        self.firmware_prepare_requested.emit(
            self._firmware_release, self._snapshot.pio_env, self._snapshot.hw_model
        )

    def _confirm_flash(self, full: bool) -> None:
        if self._snapshot is None or self._firmware_bundle is None:
            return
        if full:
            phrase = f"ERASE {self._snapshot.pio_env}"
            text, ok = QInputDialog.getText(
                self, "Full Firmware Install",
                "This erases firmware, settings, channels, keys, and the NodeDB. "
                f"Type {phrase} to continue:",
            )
            if not ok or text != phrase:
                return
        elif not self._yes(
            "Flash Firmware Update",
            f"Flash Meshtastic {self._firmware_bundle.release.version} to "
            f"{self._snapshot.serial_port}? Settings should be preserved, but power or cable "
            "loss can require a full recovery flash.",
        ):
            return
        expected_usb = (
            self._snapshot.usb_vid, self._snapshot.usb_pid, self._snapshot.usb_serial
        )
        self._flash_update.setEnabled(False)
        self._flash_full.setEnabled(False)
        self._firmware_log.clear()
        self.firmware_flash_requested.emit(self._firmware_bundle, full, expected_usb)

    def _rebuild_sections(self, sections) -> None:
        while self._section_stack.count():
            widget = self._section_stack.widget(0)
            self._section_stack.removeWidget(widget)
            widget.deleteLater()
        self._section_selector.clear()
        self._section_widgets.clear()
        for section in sections:
            container = QWidget()
            form = QFormLayout(container)
            widgets = {}
            for field in section.fields:
                widget = self._widget_for_field(field)
                form.addRow(field.label, widget)
                widgets[field.name] = (field, widget)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(container)
            self._section_stack.addWidget(scroll)
            self._section_selector.addItem(section.label, section.name)
            self._section_widgets[section.name] = widgets

    @staticmethod
    def _widget_for_field(field):
        if field.kind == "bool" and not field.repeated:
            widget = QCheckBox()
            widget.setChecked(bool(field.value))
        elif field.kind == "enum" and not field.repeated:
            widget = QComboBox()
            for choice in field.choices:
                widget.addItem(choice.label, choice.value)
            index = widget.findData(int(field.value))
            widget.setCurrentIndex(max(0, index))
        else:
            widget = QLineEdit()
            if field.repeated:
                widget.setText(", ".join(str(value) for value in field.value))
            elif not field.write_only and not field.read_only:
                widget.setText(str(field.value))
            if field.write_only:
                widget.setEchoMode(QLineEdit.EchoMode.Password)
                widget.setPlaceholderText("Stored on radio — enter to replace")
        widget.setEnabled(not field.read_only)
        if field.read_only:
            widget.setToolTip("Cryptographic key material is intentionally not displayed or edited.")
        return widget

    def _on_section_changed(self, index: int) -> None:
        if index >= 0:
            self._section_stack.setCurrentIndex(index)

    def _save_current_section(self) -> None:
        section = self._section_selector.currentData()
        if not section:
            return
        try:
            changes = {
                name: self._read_widget(field, widget)
                for name, (field, widget) in self._section_widgets[section].items()
                if not field.read_only
            }
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Setting", str(exc))
            return
        self.save_section_requested.emit(section, changes)

    @staticmethod
    def _read_widget(field, widget):
        if field.kind == "bool" and not field.repeated:
            return widget.isChecked()
        if field.kind == "enum" and not field.repeated:
            return widget.currentData()
        text = widget.text().strip()
        if field.write_only and not text:
            return ""
        if field.repeated:
            if not text:
                return []
            parts = [part.strip() for part in text.split(",")]
            if field.kind == "int":
                return [int(part, 0) for part in parts]
            if field.kind == "float":
                return [float(part) for part in parts]
            return parts
        if field.kind == "int":
            return int(text, 0)
        if field.kind == "float":
            return float(text)
        return text

    def _rebuild_channels(self, channels) -> None:
        self._channel_by_index = {channel.index: channel for channel in channels}
        self._channel_selector.clear()
        for channel in channels:
            self._channel_selector.addItem(
                f"{channel.index}: {channel.name or channel.role_name}", channel.index
            )
        self._load_channel(0)

    def _load_channel(self, _index: int) -> None:
        channel = self._channel_by_index.get(self._channel_selector.currentData())
        if channel is None:
            return
        self._channel_name.setText(channel.name)
        self._channel_role.setCurrentIndex(max(0, self._channel_role.findData(channel.role)))
        self._channel_uplink.setChecked(channel.uplink_enabled)
        self._channel_downlink.setChecked(channel.downlink_enabled)
        self._position_precision.setValue(channel.position_precision)
        self._channel_psk.clear()

    def _save_channel(self) -> None:
        index = self._channel_selector.currentData()
        if index is None:
            return
        self.channel_requested.emit({
            "index": index,
            "name": self._channel_name.text(),
            "role": self._channel_role.currentData(),
            "uplink_enabled": self._channel_uplink.isChecked(),
            "downlink_enabled": self._channel_downlink.isChecked(),
            "position_precision": self._position_precision.value(),
            "psk": self._channel_psk.text(),
        })

    def _set_fixed_position(self) -> None:
        try:
            latitude = float(self._latitude.text())
            longitude = float(self._longitude.text())
        except ValueError:
            QMessageBox.warning(self, "Invalid Position", "Latitude and longitude must be numbers.")
            return
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            QMessageBox.warning(self, "Invalid Position", "Coordinates are outside valid ranges.")
            return
        self.fixed_position_requested.emit(latitude, longitude, self._altitude.value())

    def _confirm_reboot(self) -> None:
        if self._yes("Reboot Radio", "Reboot the connected radio now?"):
            self.reboot_requested.emit(2)

    def _confirm_shutdown(self) -> None:
        if self._yes("Shutdown Radio", "Shut down the connected radio?"):
            self.shutdown_requested.emit(2)

    def _confirm_nodedb_reset(self) -> None:
        if self._yes("Reset Node Database", "Delete the radio's learned NodeDB? It will repopulate over time."):
            self.reset_nodedb_requested.emit()

    def _confirm_factory_reset(self, full: bool = False) -> None:
        word = "ERASE" if full else "RESET"
        text, ok = QInputDialog.getText(
            self,
            "Full Factory Reset" if full else "Factory Reset",
            f"This removes radio configuration. Type {word} to continue:",
        )
        if ok and text == word:
            self.factory_reset_requested.emit(full)

    def _yes(self, title: str, text: str) -> bool:
        return QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) == QMessageBox.StandardButton.Yes
