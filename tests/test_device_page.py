from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QFileDialog, QInputDialog, QPushButton
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
        "Overview", "Configuration", "Channels", "Firmware", "Maps",
    ]


def test_device_page_enables_usb_controls_for_serial_snapshot():
    page = DevicePage()
    assert not page._tabs.isTabEnabled(0)
    assert page._tabs.isTabEnabled(3)
    page.set_snapshot(_snapshot())
    assert page._tabs.isTabEnabled(0)
    assert "COM8" in page._summary.text()
    assert "tbeam-s3-core" in page._summary.text()


def _pack(**overrides) -> SimpleNamespace:
    """Stands in for services.orcmaps.OrcMapsPack in UI-only tests."""
    fields = {
        "display_name": "Oregon regional",
        "zoom_label": "z1-13",
        "pack_class": "open",
        "size_bytes": 84_615_534,
        "region_name": "Oregon, USA",
        "attribution_text": "© OpenMapTiles · © OpenStreetMap contributors",
        "manifest": Path("oregon.manifest.json"),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_maps_tab_lists_packs_with_provenance():
    page = DevicePage()

    page.set_map_packs([_pack()], tools_available=True, reason="")

    assert page._pack_table.rowCount() == 1
    assert page._pack_table.item(0, 0).text() == "Oregon regional"
    assert page._pack_table.item(0, 2).text() == "open"
    assert page._pack_table.item(0, 3).text() == "85 MB"
    # Attribution has to be visible here too, not only on the map.
    assert "OpenStreetMap" in page._pack_table.item(0, 5).text()
    assert "1 pack(s)" in page._maps_status.text()
    assert page._maps_cut.isEnabled()


def test_maps_tab_explains_itself_when_orcmaps_is_missing():
    page = DevicePage()

    page.set_map_packs([], tools_available=False, reason="OrcMaps host tools were not found.")

    assert page._pack_table.rowCount() == 0
    assert "not found" in page._maps_status.text()
    assert not page._maps_cut.isEnabled()
    assert not page._maps_verify.isEnabled()


def test_maps_tab_verify_emits_the_chosen_directory(monkeypatch):
    page = DevicePage()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_a, **_k: r"G:\card")
    requested = []
    page.maps_verify_requested.connect(requested.append)

    page._verify_maps_directory()

    assert requested == [r"G:\card"]


def test_cut_pack_without_a_source_pack_explains_instead_of_emitting(monkeypatch):
    page = DevicePage()
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: shown.append(args))
    emitted = []
    page.maps_provision_requested.connect(emitted.append)

    page._cut_pack()

    assert shown, "the user must be told why nothing happened"
    assert not emitted


def test_maps_operation_completed_reports_and_unbusies():
    page = DevicePage()
    page.set_map_packs([_pack()], tools_available=True, reason="")
    page.set_maps_busy(True)

    page.maps_operation_completed("verify", False, "A device would not use this card")

    assert page._maps_verify.isEnabled()
    assert "would not use" in page._maps_status.text()
    assert "ERROR:" in page._maps_log.toPlainText()


def test_busy_toggle_does_not_resurrect_verify_without_tools():
    page = DevicePage()
    page.set_map_packs([_pack()], tools_available=False, reason="OrcMaps host tools were not found.")
    assert not page._maps_verify.isEnabled()

    page.set_maps_busy(True)
    page.set_maps_busy(False)

    assert not page._maps_verify.isEnabled()
    # Cutting is still possible: it needs a source pack, not the host tools.
    assert page._maps_cut.isEnabled()


def test_busy_state_disables_every_maps_action():
    page = DevicePage()
    page.set_map_packs([_pack()], tools_available=True, reason="")

    page.set_maps_busy(True)

    assert not page._maps_rescan.isEnabled()
    assert not page._maps_verify.isEnabled()
    assert not page._maps_cut.isEnabled()


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
