"""Tests for ConnectionBar — transport switching and the BLE device list.

The Bluetooth transport's failure mode was silence: the device combo was only
ever filled by a scan, so after a restart (or on a first switch to Bluetooth)
it was empty, Connect had nothing to send, and nothing at all happened — no
error, no scan, no message. These tests pin the behaviour that replaced it.
"""
from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication
import sys

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.controllers.meshtastic_controller import ConnectionState
from meshchat.models.connection_profile import ConnectionProfile
from meshchat.ui.widgets.connection_bar import ConnectionBar

_BLE = "44:1B:F6:6F:81:BD"


def _profile(**overrides) -> ConnectionProfile:
    fields = {"transport": "ble", "ble_address": _BLE}
    fields.update(overrides)
    return ConnectionProfile(**fields)  # type: ignore[arg-type]


def _device(name: str, address: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, address=address)


def _connect_and_collect(bar: ConnectionBar) -> tuple[list, list]:
    """Press Connect; return (ble addresses requested, scans requested)."""
    addresses: list = []
    scans: list = []
    bar.connect_ble_requested.connect(addresses.append)
    bar.scan_requested.connect(lambda: scans.append(True))
    bar._on_connect()
    return addresses, scans


# ── Restoring the saved profile ─────────────────────────────────────────────


def test_a_saved_ble_address_is_restored_into_the_combo():
    """Without this the combo is empty on launch and Connect does nothing."""
    bar = ConnectionBar()

    bar.restore_profile(_profile())

    assert bar._ble_combo.currentData() == _BLE


def test_a_restored_ble_device_can_be_connected_without_scanning_first():
    bar = ConnectionBar()
    bar.restore_profile(_profile())

    addresses, scans = _connect_and_collect(bar)

    assert addresses == [_BLE]
    assert scans == [], "a restored address should not force a scan"


def test_restoring_a_ble_profile_does_not_start_a_scan():
    """The saved device is already known; scanning is not needed to use it."""
    bar = ConnectionBar()
    scans: list = []
    bar.scan_requested.connect(lambda: scans.append(True))

    bar.restore_profile(_profile())

    assert scans == []


def test_a_restored_address_is_labelled_as_last_used():
    """We don't know the device's advertised name until a scan runs."""
    bar = ConnectionBar()

    bar.restore_profile(_profile())

    assert "Last used" in bar._ble_combo.currentText()
    assert _BLE in bar._ble_combo.currentText()


def test_a_serial_profile_is_still_restored():
    bar = ConnectionBar()

    bar.restore_profile(ConnectionProfile(transport="serial", serial_port="COM24"))

    assert bar._serial_combo.currentData() == "COM24"
    assert bar._transport.currentIndex() == 2


# ── Pressing Connect with nothing selected ──────────────────────────────────


def test_connect_with_no_ble_device_scans_instead_of_doing_nothing():
    bar = ConnectionBar()
    assert bar._ble_combo.currentData() is None

    addresses, scans = _connect_and_collect(bar)

    assert addresses == []
    assert scans == [True], "Connect must not be a silent no-op"


def test_switching_to_bluetooth_with_no_devices_starts_a_scan():
    """Mirrors the serial transport, which already enumerates ports on switch."""
    bar = ConnectionBar()
    scans: list = []
    bar.scan_requested.connect(lambda: scans.append(True))

    bar._transport.setCurrentIndex(0)
    bar._on_transport_changed(0)

    assert scans != []


# ── Scan results ────────────────────────────────────────────────────────────


def test_scan_results_populate_the_combo_with_name_and_address():
    bar = ConnectionBar()

    bar.set_ble_devices([_device("hrdc_81bc", _BLE)])

    assert bar._ble_combo.count() == 1
    assert bar._ble_combo.currentData() == _BLE
    assert "hrdc_81bc" in bar._ble_combo.currentText()


def test_a_rescan_keeps_the_previously_chosen_device():
    """Clearing the combo on every scan would lose the user's selection."""
    bar = ConnectionBar()
    bar.set_ble_devices([
        _device("other", "AA:AA:AA:AA:AA:AA"),
        _device("hrdc_81bc", _BLE),
    ])
    bar._ble_combo.setCurrentIndex(bar._ble_combo.findData(_BLE))

    bar.set_ble_devices([
        _device("unknown-first", "BB:BB:BB:BB:BB:BB"),
        _device("hrdc_81bc", _BLE),
    ])

    assert bar._ble_combo.currentData() == _BLE


def test_a_rescan_after_restore_still_selects_the_saved_device():
    bar = ConnectionBar()
    bar.restore_profile(_profile())

    bar.set_ble_devices([_device("hrdc_81bc", _BLE)])

    assert bar._ble_combo.currentData() == _BLE
    assert "hrdc_81bc" in bar._ble_combo.currentText()


def test_connecting_records_the_choice_for_the_next_scan():
    bar = ConnectionBar()
    bar.set_ble_devices([_device("hrdc_81bc", _BLE)])

    _connect_and_collect(bar)

    assert bar._preferred_ble_address == _BLE


# ── Controls track connection state ─────────────────────────────────────────


def test_controls_are_disabled_while_connecting():
    bar = ConnectionBar()

    bar.set_state(ConnectionState.CONNECTING)

    assert not bar._ble_combo.isEnabled()
    assert not bar._scan_btn.isEnabled()
    assert not bar._transport.isEnabled()


# ── A slow Bluetooth connect must not look like a hang ──────────────────────


def test_connecting_over_bluetooth_says_it_is_slow():
    """It takes ~35s (scan + pairing + config sync); silence reads as failure."""
    bar = ConnectionBar()
    bar._transport.setCurrentIndex(0)

    bar.set_state(ConnectionState.CONNECTING, _BLE)

    assert "Bluetooth" in bar._status_label.text()
    assert bar.connecting_expectation() != ""


def test_connecting_over_serial_makes_no_such_claim():
    bar = ConnectionBar()
    bar._transport.setCurrentIndex(2)

    bar.set_state(ConnectionState.CONNECTING, "COM24")

    assert bar._status_label.text() == "Connecting…"
    assert bar.connecting_expectation() == ""


def test_the_state_detail_is_kept_in_the_tooltip():
    """The detail was accepted and silently discarded before."""
    bar = ConnectionBar()
    bar._transport.setCurrentIndex(0)

    bar.set_state(ConnectionState.CONNECTING, _BLE)

    assert _BLE in bar._status_label.toolTip()


def test_current_transport_reports_the_selection():
    bar = ConnectionBar()

    for index, expected in enumerate(("ble", "tcp", "serial")):
        bar._transport.setCurrentIndex(index)
        assert bar.current_transport() == expected
