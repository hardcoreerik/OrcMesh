from meshchat.models.device_control import DeviceControlSnapshot
from meshchat.ui.device.device_page import DevicePage


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
