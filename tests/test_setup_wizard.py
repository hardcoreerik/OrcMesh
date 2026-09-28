"""Widget tests for the setup wizard.

The wizard owns no services, so everything here runs without a radio, without
OrcMaps and without a MainWindow — which is the point of the API it exposes.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from PySide6.QtWidgets import QDialogButtonBox

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.services.orcmaps import OrcMapsPack, OrcMapsTools
from meshchat.services.provisioning.capability import detect_capability
from meshchat.services.provisioning.destinations import describe_destination
from meshchat.ui.setup.setup_wizard import SetupWizard


def _pump(ms: int = 30) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _pack(stem: str, *, max_zoom: int, size_bytes: int, display_name: str = "") -> OrcMapsPack:
    return OrcMapsPack(
        stem=stem,
        pmtiles=Path(f"{stem}.pmtiles"),
        manifest=Path(f"{stem}.manifest.json"),
        sha256=None,
        display_name=display_name or stem,
        region_name="Oregon, USA",
        min_zoom=1,
        max_zoom=max_zoom,
        pack_class="open",
        pack_version="1",
        schema_version="openmaptiles-3.16",
        content_profile="standard",
        attribution=("© OpenStreetMap contributors",),
        attribution_links=("https://www.openstreetmap.org/copyright",),
        bounds=(-124.0, 42.0, -122.0, 45.0),
        size_bytes=size_bytes,
        output_sha256=None,
        priority=10,
    )


def _tools(tmp_path: Path) -> OrcMapsTools:
    return OrcMapsTools(
        home=tmp_path,
        inspect=tmp_path / "orcmap_pack_inspect.exe",
        verify=tmp_path / "orcmap_pack_verify.exe",
        provision_script=tmp_path / "provision_pack.py",
        pmtiles_cli=tmp_path / "pmtiles.exe",
    )


#: Eugene, Oregon — a real fix, since a region cannot be cut without one.
_EUGENE = (44.0521, -123.0922)


def _wizard(tmp_path, *, deep: bool = True, catalog: bool = False, centre=_EUGENE) -> SetupWizard:
    packs = [_pack("oregon", max_zoom=13 if deep else 9, size_bytes=84_000_000,
                   display_name="Oregon regional")]
    capability = detect_capability(_tools(tmp_path), packs, catalog_available=catalog)
    wizard = SetupWizard(capability)
    if centre is not None:
        wizard.set_centre(*centre)
    return wizard


def _ok_button(wizard: SetupWizard):
    return wizard._ok


class TestSteps:
    def test_it_opens_on_the_capability_step(self, tmp_path):
        wizard = _wizard(tmp_path)

        assert wizard._stack.currentIndex() == 0
        assert "Step 1 of 3" in wizard._step_label.text()

    def test_the_first_button_says_next_and_the_last_says_finish(self, tmp_path):
        wizard = _wizard(tmp_path)
        assert _ok_button(wizard).text() == "Next"

        _ok_button(wizard).click()
        _pump()
        assert wizard._stack.currentIndex() == 1
        assert _ok_button(wizard).text() == "Next"

        _ok_button(wizard).click()
        _pump()
        assert wizard._stack.currentIndex() == 2
        assert _ok_button(wizard).text() == "Finish"

    def test_back_walks_the_steps_and_is_disabled_on_the_first(self, tmp_path):
        wizard = _wizard(tmp_path)
        assert not wizard._back.isEnabled()

        _ok_button(wizard).click()
        _pump()
        assert wizard._back.isEnabled()

        wizard._back.click()
        _pump()
        assert wizard._stack.currentIndex() == 0

    def test_back_never_goes_below_the_first_step(self, tmp_path):
        wizard = _wizard(tmp_path)

        wizard._back.click()
        wizard._back.click()

        assert wizard._stack.currentIndex() == 0

    def test_cancel_rejects_the_dialog(self, tmp_path):
        wizard = _wizard(tmp_path)
        seen = []
        wizard.rejected.connect(lambda: seen.append(True))

        wizard._buttons.rejected.emit()

        assert seen == [True]


class TestCapabilityPage:
    def test_it_states_what_will_happen(self, tmp_path):
        wizard = _wizard(tmp_path)

        assert "Oregon regional" in wizard._capability_summary.text()

    def test_a_missing_prerequisite_is_spelled_out(self, tmp_path):
        capability = detect_capability(None, [])
        wizard = SetupWizard(capability)

        assert "not found" in wizard._capability_reasons.text()
        assert _ok_button(wizard).isEnabled() is False, (
            "an impossible setup must not offer to continue"
        )

    def test_downloading_is_offered_when_building_is_not(self, tmp_path):
        capability = detect_capability(None, [], catalog_available=True)
        wizard = SetupWizard(capability)

        assert _ok_button(wizard).isEnabled() is True
        assert "downloaded" in wizard._capability_summary.text()


class TestRadioPage:
    def test_serial_ports_fill_the_list(self, tmp_path):
        wizard = _wizard(tmp_path)
        from meshchat.controllers.meshtastic_controller import SerialPortSummary

        wizard.set_serial_ports([
            SerialPortSummary(device="COM24", description="USB Serial Device"),
            SerialPortSummary(device="COM7", description=""),
        ])

        assert wizard._device.count() == 2
        assert wizard._device.currentData() == "COM24"
        assert "COM24" in wizard._device.itemText(0)
        assert "2 port(s)" in wizard._radio_hint.text()

    def test_no_ports_says_so_instead_of_showing_an_empty_box(self, tmp_path):
        wizard = _wizard(tmp_path)

        wizard.set_serial_ports([])

        assert wizard._device.count() == 0
        assert "plugged in" in wizard._radio_hint.text()

    def test_bluetooth_devices_fill_the_list(self, tmp_path):
        wizard = _wizard(tmp_path)
        from meshchat.controllers.meshtastic_controller import BleDeviceSummary

        wizard._transport.setCurrentIndex(1)
        wizard.set_ble_devices([BleDeviceSummary(name="hrdc_81bc", address="44:1B:F6:6F:81:BD")])

        assert wizard._device.currentData() == "44:1B:F6:6F:81:BD"
        assert "hrdc_81bc" in wizard._device.itemText(0)

    def test_bluetooth_scan_says_how_long_it_takes(self, tmp_path):
        wizard = _wizard(tmp_path)

        wizard._transport.setCurrentIndex(1)

        assert "10 seconds" in wizard._radio_hint.text()

    def test_switching_transport_clears_a_stale_list(self, tmp_path):
        """A COM port must never be offered as a Bluetooth address."""
        wizard = _wizard(tmp_path)
        from meshchat.controllers.meshtastic_controller import SerialPortSummary

        wizard.set_serial_ports([SerialPortSummary(device="COM24", description="Serial")])
        wizard._transport.setCurrentIndex(1)

        assert wizard._device.count() == 0

    def test_the_selected_transport_and_target_are_reported(self, tmp_path):
        wizard = _wizard(tmp_path)
        from meshchat.controllers.meshtastic_controller import SerialPortSummary

        wizard.set_serial_ports([SerialPortSummary(device="COM24", description="Serial")])
        assert wizard.selected_device() == ("serial", "COM24")

        wizard._transport.setCurrentIndex(1)
        wizard.set_ble_devices([])
        assert wizard.selected_device()[0] == "ble"


class TestRegionsPage:
    def test_the_estimate_appears_as_the_form_is_filled(self, tmp_path):
        wizard = _wizard(tmp_path)
        wizard._show_step(2)

        assert "Give the region a name" in wizard._estimate_label.text()

        wizard._region_name.setText("lane-county")
        wizard._update_estimate()

        assert "MB" in wizard._estimate_label.text()
        assert "84 MB" in wizard._estimate_label.text(), "the basis must be shown"

    def test_a_bigger_radius_estimates_more(self, tmp_path):
        wizard = _wizard(tmp_path)
        wizard._region_name.setText("lane-county")

        wizard._radius.setValue(25.0)
        small = wizard._estimate_label.text()
        wizard._radius.setValue(100.0)
        big = wizard._estimate_label.text()

        assert small != big

    def test_adding_a_region_fills_the_table(self, tmp_path):
        wizard = _wizard(tmp_path)
        wizard._region_name.setText("lane-county")

        wizard._add_current_region()

        assert wizard._region_table.rowCount() == 1
        assert wizard._region_table.item(0, 0).text() == "lane-county"
        assert "Not installed yet" in wizard._region_table.item(0, 3).text()

    def test_an_unnamed_region_is_not_added(self, tmp_path):
        wizard = _wizard(tmp_path)

        wizard._add_current_region()

        assert wizard._region_table.rowCount() == 0
        assert wizard.regions() == []

    def test_both_detail_levels_can_be_added(self, tmp_path):
        wizard = _wizard(tmp_path)
        wizard._region_name.setText("lane-county")
        wizard._add_current_region()
        wizard._region_name.setText("eugene")
        wizard._tier.setCurrentIndex(1)
        wizard._add_current_region()

        assert [region["tier"] for region in wizard.regions()] == ["region", "local"]
        assert wizard._region_table.rowCount() == 2

    def test_finishing_with_a_filled_form_installs_what_was_typed(self, tmp_path):
        """The last typed region must not be silently dropped."""
        wizard = _wizard(tmp_path)
        wizard._show_step(2)
        wizard._region_name.setText("lane-county")
        captured = []
        wizard.regions_accepted.connect(captured.append)

        _ok_button(wizard).click()
        _pump()

        assert captured and captured[0][0]["name"] == "lane-county"

    def test_finishing_reports_every_added_region(self, tmp_path):
        wizard = _wizard(tmp_path)
        wizard._show_step(2)
        wizard._region_name.setText("lane-county")
        wizard._add_current_region()
        captured = []
        wizard.regions_accepted.connect(captured.append)

        _ok_button(wizard).click()
        _pump()

        assert captured == [[wizard.regions()[0]]]

    def test_each_region_carries_its_estimate_and_basis(self, tmp_path):
        wizard = _wizard(tmp_path)
        wizard._region_name.setText("lane-county")
        wizard._add_current_region()

        region = wizard.regions()[0]

        assert region["estimate_bytes"] > 0
        assert region["estimate_basis"]
        assert region["max_zoom"] == 13

    def test_a_region_without_a_centre_is_refused(self, tmp_path):
        """OrcMaps cuts around a point; guessing one would cut the wrong place."""
        wizard = _wizard(tmp_path, centre=None)
        wizard._region_name.setText("lane-county")

        wizard._add_current_region()

        assert wizard.regions() == []
        assert "centre" in wizard._estimate_label.text()

    def test_the_centre_is_remembered_on_the_region(self, tmp_path):
        wizard = _wizard(tmp_path)
        wizard._region_name.setText("lane-county")

        wizard._add_current_region()

        assert wizard.regions()[0]["lat"] == _EUGENE[0]
        assert wizard.regions()[0]["lon"] == _EUGENE[1]

    def test_a_radio_with_no_fix_does_not_set_a_centre(self, tmp_path):
        """(0, 0) is how a radio reports "no position", and it is not a place."""
        wizard = _wizard(tmp_path, centre=None)

        wizard.set_centre(0.0, 0.0)

        wizard._region_name.setText("lane-county")
        wizard._add_current_region()
        assert wizard.regions() == []


class TestDestination:
    def test_it_defaults_to_the_app_map_directory(self, tmp_path):
        wizard = _wizard(tmp_path)

        assert "orcmaps" in wizard._destination_label.text()

    def test_a_pack_directory_is_described_differently(self, tmp_path):
        pack_dir = tmp_path / "orcmaps"
        pack_dir.mkdir()
        (pack_dir / "oregon.manifest.json").write_text("{}", encoding="utf-8")
        wizard = _wizard(tmp_path)

        wizard.set_destination(pack_dir)

        assert "pack directory" in wizard._destination_label.text()
        assert wizard.destination() == pack_dir

    def test_a_plain_folder_is_treated_as_a_card_root(self, tmp_path):
        card = tmp_path / "CARD"
        card.mkdir()
        wizard = _wizard(tmp_path)

        wizard.set_destination(card)

        assert "card root" in wizard._destination_label.text()

    def test_describe_destination_handles_a_missing_folder(self, tmp_path):
        assert "card root" in describe_destination(tmp_path / "nope")


class TestStandaloneConcerns:
    def test_the_wizard_builds_without_any_tools_at_all(self, tmp_path):
        """The 'everybody' path: no OrcMaps, no Java, no source archive."""
        capability = detect_capability(None, [], catalog_available=True)

        wizard = SetupWizard(capability)

        assert wizard.selected_device() == ("serial", "")

    def test_an_ok_button_is_present_for_accepting(self, tmp_path):
        wizard = _wizard(tmp_path)

        assert wizard._buttons.button(QDialogButtonBox.StandardButton.Ok) is wizard._ok
