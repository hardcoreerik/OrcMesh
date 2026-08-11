from types import SimpleNamespace

from PySide6.QtWidgets import QInputDialog, QPushButton
from PySide6.QtWidgets import QMessageBox

from meshchat.models.device_control import ConfigChoice, ConfigField, DeviceControlSnapshot
from meshchat.ui.device.device_page import DevicePage
from meshchat.ui.main_window import MainWindow


def _snapshot() -> DeviceControlSnapshot:
    return DeviceControlSnapshot(
        node_id="!12345678",
        long_name="OrcMesh Radio",
        short_name="ORC",
        hw_model="LILYGO_TBEAM_S3_CORE",
        firmware_version="2.7.10",
        pio_env="tbeam-s3-core",
        serial_port="COM8",
        usb_vid=0x303A,
        usb_pid=0x1001,
        usb_serial=None,
        can_shutdown=True,
        has_wifi=True,
        has_bluetooth=True,
    )


def test_device_page_exposes_all_control_tabs():
    page = DevicePage()
    assert [page._tabs.tabText(i) for i in range(page._tabs.count())] == [
        "Overview", "Configuration", "Channels", "Firmware",
    ]


def test_device_page_enables_usb_controls_for_serial_snapshot():
    page = DevicePage()
    assert not page._tabs.isEnabled()
    page.set_snapshot(_snapshot())
    assert page._tabs.isEnabled()
    assert "COM8" in page._summary.text()
    assert "tbeam-s3-core" in page._summary.text()


def test_unknown_enum_value_is_preserved():
    field = ConfigField(
        name="mode", label="Mode", kind="enum", value=99,
        choices=(ConfigChoice("KNOWN", 1),),
    )
    widget = DevicePage._widget_for_field(field)
    assert widget.currentData() == 99
    assert DevicePage._read_widget(field, widget) == 99


def test_factory_reset_button_emits_non_full_reset(monkeypatch):
    page = DevicePage()
    page.set_snapshot(_snapshot())
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args: ("RESET", True))
    requested = []
    page.factory_reset_requested.connect(requested.append)
    button = next(button for button in page.findChildren(QPushButton) if button.text() == "Factory Reset")
    button.click()
    assert requested == [False]


def test_flash_handoff_rejects_disconnected_snapshot(monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(True))
    window = SimpleNamespace(_is_connected=False, _device_snapshot=_snapshot())
    MainWindow._on_firmware_flash_requested(window, object(), False, None)
    assert warnings == [True]
