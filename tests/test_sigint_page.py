"""Tests for the SIGINT page.

Constructing the page builds a pyqtgraph plot, two waterfall views and an OpenGL
widget, but starts no threads: the SDR and scan controllers are only created when
a capture or a scan actually begins, which is why these tests can drive the page
directly. Band pictures and packets are produced by the real analysis functions
rather than hand-built reports, so the page is tested against what will really
reach it.
"""
from __future__ import annotations

import sys

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv[:1])

from datetime import datetime, timedelta  # noqa: E402

from meshchat.analytics.lora_bands import meshtastic_markers  # noqa: E402
from meshchat.analytics.sdr_presets import default_preset  # noqa: E402
from meshchat.analytics.slot_occupancy import rank_slots  # noqa: E402
from meshchat.services import rtl_tools  # noqa: E402
from meshchat.services.rtl_tools import SdrDevice  # noqa: E402
from meshchat.models.network_packet import NetworkPacket  # noqa: E402
from meshchat.services.rtl_scan import BandPicture, PowerRow, ScanRequest  # noqa: E402
from meshchat.ui.sigint.sigint_page import SigintPage  # noqa: E402

_BASE = datetime(2026, 9, 27, 12, 0, 0)
_BIN_HZ = 40_625.0
_LOW_HZ = 902.0e6
_BINS = 641
_ALPHA = 0x11111111
_BRAVO = 0x22222222


def _page() -> SigintPage:
    return SigintPage()


def _band_picture(*, peak_hz: float | None = 907.4e6, peak_db: float = -20.0) -> BandPicture:
    power = np.full(_BINS, -75.0, dtype=np.float32)
    if peak_hz is not None:
        index = int((peak_hz - _LOW_HZ) / _BIN_HZ)
        if 0 <= index < _BINS:
            power[index] = peak_db
    row = PowerRow(_LOW_HZ, _LOW_HZ + (_BINS - 1) * _BIN_HZ, _BIN_HZ, 256, power)
    ages = np.zeros(_BINS, dtype=np.int32)
    return BandPicture(row=row, coverage=1.0, sweeps=4, ages=ages)


def _packet(
    *,
    sender: int | None = _ALPHA,
    minutes: float = 0.0,
    payload: int | None = 32,
    snr: float | None = 4.0,
    hops: int | None = 0,
    via_mqtt: bool = False,
) -> NetworkPacket:
    return NetworkPacket(
        session_id="s", observed_at=_BASE + timedelta(minutes=minutes), rx_time=None,
        sender_num=sender, sender_id=None, destination_num=None, packet_id=None,
        channel_index=0, portnum=1, portnum_name="TEXT_MESSAGE_APP", text=None,
        payload_size=payload, rx_snr=snr, rx_rssi=-70, hop_start=3,
        hop_limit=(3 - hops) if hops is not None else None, hops_used=hops,
        via_mqtt=via_mqtt, transport_mechanism=None, pki_encrypted=None,
        want_ack=None, priority=None, raw_metadata_json=None,
    )


class TestConstruction:
    def test_the_page_builds(self):
        page = _page()

        assert page._capture_btn.text() == "Start"
        assert page._scan_btn.text() == "Scan region"

    def test_no_controllers_are_created_until_something_starts(self):
        """Each controller starts a QThread, so an idle page must not make one."""
        page = _page()

        assert page._sdr is None
        assert page._scan is None

    def test_it_opens_on_the_us_channel_at_plus_minus_one_mhz(self):
        """906.875 MHz is US slot 19 at 250 kHz; the rate is the span, so 2.0 MS/s
        is the ±1 MHz window around it."""
        page = _page()

        assert page._center.value() == pytest.approx(906.875)
        assert page._rate.value() == pytest.approx(2.0)
        assert page._region.currentData() == "US"

    def test_the_displayed_span_is_the_centre_plus_minus_one_mhz(self):
        """Both waterfall axes are built from centre ± span/2, so the defaults had
        better put 905.875-907.875 MHz on screen."""
        page = _page()
        center_hz = page._center.value() * 1e6
        span_hz = page._rate.value() * 1e6

        page._waterfall2d.configure(center_hz, span_hz, 1024)
        page._waterfall3d.configure(center_hz, span_hz, 1024)

        assert page._waterfall2d._center_hz == pytest.approx(906.875e6)
        assert page._waterfall2d._span_hz == pytest.approx(2.0e6)
        assert page._waterfall3d._span_hz == pytest.approx(2.0e6)
        half = page._waterfall2d._span_hz / 2
        assert page._waterfall2d._center_hz - half == pytest.approx(905.875e6)
        assert page._waterfall2d._center_hz + half == pytest.approx(907.875e6)

    def test_the_centre_control_steps_by_a_250_khz_slot(self):
        """Landing between channels is not a useful place for the arrows to stop."""
        assert _page()._center.singleStep() == pytest.approx(0.25)

    def test_the_region_list_comes_from_the_band_plan(self):
        page = _page()

        assert page._region.count() > 0
        assert page._region.currentData()

    def test_the_default_gain_is_not_auto(self):
        """Auto resolves to near-maximum, the worst case for headroom."""
        assert _page()._gain.value() > 0.0

    def test_the_default_bin_size_resolves_a_lora_slot(self):
        """A 125 kHz slot is unreadable if the bins are near its width."""
        assert _page()._bin.value() < 125.0

    def test_it_says_what_recording_will_cost(self):
        page = _page()

        hint = page._record_btn.toolTip()

        assert "MB/s" in hint and "per minute" in hint

    def test_shutdown_without_starting_anything_is_safe(self):
        _page().shutdown()


class TestBandSurvey:
    def test_a_band_picture_draws_and_ranks_slots(self):
        page = _page()

        page._on_band(_band_picture())

        assert page._band_curve.getData()[0] is not None
        assert page._slot_table.rowCount() > 0
        assert "measured" in page._coverage.text()

    def test_it_reports_the_coverage_it_actually_has(self):
        page = _page()

        page._on_band(_band_picture())

        assert "100%" in page._coverage.text()
        assert "4 sweeps" in page._coverage.text()

    def test_the_strongest_slot_is_listed_first(self):
        """A signal dropped into a slot must appear at the top of the table."""
        page = _page()
        picture = _band_picture(peak_hz=927.0e6, peak_db=-15.0)

        page._on_band(picture)

        report = rank_slots(
            picture.row.frequencies,
            picture.row.power_db,
            meshtastic_markers("US", None, 0, include_neighbours=14),
        )

        assert report.hottest is not None
        assert report.hottest.peak_over_floor_db > 20.0
        assert page._slot_table.rowCount() > 0

    def test_a_slot_with_no_data_shows_a_dash_not_a_zero(self):
        """A slot nobody measured is not a quiet slot.

        Almost the whole band is left unmeasured here on purpose: unmeasured slots
        sort last, so they only reach a table this size if few slots were measured
        at all.
        """
        page = _page()
        picture = _band_picture(peak_hz=None)
        picture.row.power_db[:] = np.nan
        picture.row.power_db[320] = -20.0

        page._on_band(picture)

        peaks = [
            page._slot_table.item(row, 2).text()
            for row in range(page._slot_table.rowCount())
        ]
        assert "—" in peaks, "unmeasured slots must not be shown as a value"

    def test_an_empty_band_does_not_break_the_table(self):
        page = _page()

        page._on_band(_band_picture(peak_hz=None))

        assert page._slot_table.rowCount() > 0

    def test_rows_pushed_before_the_3d_surface_exists_are_kept(self):
        """The pane must show the capture's history, not a blank grid, when it
        comes up mid-capture."""
        page = _page()
        page._waterfall3d.configure(915e6, 2.4e6, 8)

        for index in range(5):
            page._on_row(np.full(8, float(index), dtype=np.float32))

        assert page._waterfall3d._history is not None
        assert page._waterfall3d._history[-1, 0] == pytest.approx(4.0)


class TestPacketIntelligence:
    def test_packets_populate_the_table(self):
        page = _page()
        packets = [_packet(minutes=index) for index in range(4)]

        page.set_packets(packets)

        assert page._intel_table.rowCount() == 1
        assert page._intel_table.item(0, 3).text() == "4"

    def test_no_traffic_says_so_rather_than_showing_an_empty_table(self):
        page = _page()
        page.set_packets([_packet()])

        page.set_packets([])

        assert page._intel_table.rowCount() == 0
        assert "no traffic yet" in page._intel_summary.text()

    def test_a_name_is_used_when_one_is_known(self):
        page = _page()

        page.set_packets([_packet()], labels={_ALPHA: "Tower"})

        assert page._intel_table.item(0, 0).text() == "Tower"

    def test_an_unknown_node_falls_back_to_its_hex_id(self):
        page = _page()

        page.set_packets([_packet()])

        assert page._intel_table.item(0, 0).text() == f"!{_ALPHA:08x}"

    def test_a_node_the_mesh_has_no_history_with_is_marked(self):
        page = _page()
        packets = [_packet(sender=_ALPHA), _packet(sender=_BRAVO)]

        page.set_packets(packets, known_nodes=[_ALPHA])

        assert "unfamiliar" in page._intel_summary.text()
        tooltips = [
            page._intel_table.item(row, 0).toolTip()
            for row in range(page._intel_table.rowCount())
        ]
        assert any("no prior record" in tip for tip in tooltips)

    def test_the_summary_reports_channel_occupancy(self):
        page = _page()

        page.set_packets([_packet(minutes=index) for index in range(4)])

        assert "channel busy" in page._intel_summary.text()

    def test_packets_with_no_payload_size_are_reported_as_an_undercount(self):
        page = _page()
        packets = [_packet(payload=32), _packet(payload=None), _packet(payload=None)]

        page.set_packets(packets)

        assert "no payload size" in page._intel_summary.text()

    def test_routing_is_shown_per_node(self):
        page = _page()
        packets = [_packet(hops=0), _packet(hops=2), _packet(hops=0, via_mqtt=True)]

        page.set_packets(packets)

        routing = page._intel_table.item(0, 7).text()
        assert "direct" in routing
        assert "relayed" in routing
        assert "mqtt" in routing

    def test_the_table_is_capped_so_a_busy_mesh_stays_readable(self):
        page = _page()
        packets = [_packet(sender=0x1000 + index) for index in range(120)]

        page.set_packets(packets)

        assert page._intel_table.rowCount() == 40

    def test_it_works_with_no_preset_rather_than_refusing(self):
        """Packets arrive from radios whose preset we were never told."""
        page = _page()

        page.set_packets([_packet()], preset=None)

        assert page._intel_table.rowCount() == 1

    def test_a_real_preset_changes_the_airtime_shown(self):
        page = _page()
        packets = [_packet(payload=64) for _ in range(3)]

        page.set_packets(packets, preset="LONG_FAST")
        fast = page._intel_table.item(0, 1).text()

        page.set_packets(packets, preset="LONG_SLOW")
        slow = page._intel_table.item(0, 1).text()

        assert fast != slow


class _StubCapture:
    """Stands in for SdrController so a capture can be driven without a dongle."""

    def __init__(self):
        self.started: list[tuple[float, float, float, int]] = []
        self.stopped = False

    def start(
        self, center_hz: float, rate_hz: float, gain_db: float, device_index: int = 0
    ) -> None:
        self.started.append((center_hz, rate_hz, gain_db, device_index))

    def stop(self) -> None:
        self.stopped = True


class _StubScan:
    """Stands in for ScanController: records the request it was handed."""

    def __init__(self):
        self.requests: list[ScanRequest] = []

    def start(self, request: ScanRequest) -> None:
        self.requests.append(request)

    def stop(self) -> None:
        pass


class TestDocked3DView:
    """The 3D surface is part of the pane, not a window to open and close.

    It was a separate window while the GL widget and the map's QWebEngineView
    could not agree on a composition API (the map reported "Failed to get a QRhi
    from the top-level widget's window" and drew nothing). `QSG_RHI_BACKEND=opengl`
    in app.py fixes that, so the two views are docked side by side. These tests pin
    the docking and the laziness that keeps a run which never opens the tab off
    OpenGL entirely.
    """

    def test_the_3d_view_is_docked_in_the_page_not_a_window(self):
        page = _page()

        assert page.isAncestorOf(page._waterfall3d)
        assert page._waterfall3d.window() is page.window()
        assert not page._waterfall3d.isWindow()

    def test_it_opens_with_no_gl_surface_built(self):
        page = _page()

        assert page._waterfall3d.active is False

    def test_showing_the_tab_brings_the_surface_up(self, monkeypatch):
        """No button to press: looking at the tab is what activates the pane."""
        page = _page()
        calls = []
        monkeypatch.setattr(page._waterfall3d, "activate", lambda: calls.append(1) or True)

        page.show()

        assert calls, "showEvent must bring the docked surface up"

    def test_starting_a_capture_brings_the_surface_up(self, monkeypatch):
        """A capture started before the tab is ever shown must still fill it, at
        the geometry on the toolbar — 906.875 MHz over ±1 MHz."""
        page = _page()
        calls = []
        stub = _StubCapture()
        monkeypatch.setattr(page._waterfall3d, "activate", lambda: calls.append(1) or True)
        monkeypatch.setattr(page, "_ensure_capture", lambda: stub)

        page._on_capture_clicked()

        assert page._capturing is True
        assert calls
        assert stub.started == [(906.875e6, 2.0e6, page._gain.value(), 0)]

    def test_the_surface_really_builds_when_asked(self):
        page = _page()
        if not page._waterfall3d.available:
            pytest.skip("no OpenGL here")

        assert page._activate_3d() is True
        assert page._waterfall3d.active is True

        page.shutdown()

    def test_an_unavailable_3d_view_explains_itself_in_the_pane(self, monkeypatch):
        """An empty pane would look like a broken screen, so the reason goes in it."""
        page = _page()
        monkeypatch.setattr(page._waterfall3d, "_available", False)
        monkeypatch.setattr(page._waterfall3d, "_reason", "PyOpenGL is not installed")

        assert page._activate_3d() is False

        assert page._waterfall3d.active is False
        assert page._waterfall3d._notice is not None
        assert "PyOpenGL" in page._waterfall3d._notice.text()

    def test_a_surface_that_fails_to_build_leaves_the_pane_in_place(self, monkeypatch):
        page = _page()

        def boom():
            raise RuntimeError("no context")

        monkeypatch.setattr(page._waterfall3d, "_build_surface", boom)

        assert page._activate_3d() is False

        assert page._waterfall3d.active is False
        assert page.isAncestorOf(page._waterfall3d)
        assert "no context" in page._waterfall3d.unavailable_reason


class TestStatusAndAvailability:
    def test_the_toolchain_state_is_shown_at_startup(self):
        page = _page()

        assert page._status.text()

    def test_buttons_follow_toolchain_availability(self):
        page = _page()

        available = page._capture_btn.isEnabled()
        reason = page._status.text()

        assert available == ("rtl_sdr found" in reason)

    def test_a_capture_error_reports_its_first_line(self):
        page = _page()

        page._on_capture_error("The dongle is already in use.\n\nMore detail here.")

        assert page._status.text() == "The dongle is already in use."
        assert page._capture_btn.text() == "Start"

    def test_a_capture_being_stopped_restores_the_button(self):
        page = _page()
        page._capturing = True
        page._capture_btn.setText("Stop")

        page._on_capture_stopped("Capture stopped")

        assert page._capturing is False
        assert page._capture_btn.text() == "Start"

    def test_a_recording_failure_is_reported_without_stopping_the_capture(self):
        page = _page()
        page._recording = True
        page._record_btn.setText("Stop rec")

        page._on_recording_failed("Cannot write to the drive")

        assert page._recording is False
        assert page._record_btn.text() == "Record"
        assert "Cannot write" in page._status.text()

    def test_a_scan_error_is_reported(self):
        page = _page()
        page._scanning = True
        page._scan_btn.setText("Stop scan")

        page._on_scan_error("rtl_power was not found on PATH")

        assert page._scanning is False
        assert page._scan_btn.text() == "Scan region"


class TestPresets:
    """The preset row: one selection sets up the whole capture."""

    def test_it_opens_on_the_default_preset(self):
        page = _page()

        assert page._preset.currentData() == default_preset().key

    def test_the_tab_defaults_are_the_default_preset(self):
        """The numbers the tab opens with and the preset must not drift apart."""
        page = _page()
        preset = default_preset()

        assert page._center.value() == pytest.approx(preset.center_mhz)
        assert page._rate.value() == pytest.approx(preset.span_mhz)
        assert page._gain.value() == pytest.approx(preset.gain_db)

    def test_picking_a_preset_sets_the_whole_capture(self):
        page = _page()

        page._preset.setCurrentIndex(page._preset.findData("meshcore-us"))

        assert page._center.value() == pytest.approx(910.525)
        assert page._rate.value() == pytest.approx(2.0)
        assert page._gain.value() > 0.0
        assert "910.525" in page._status.text(), "it says what it just set up"

    def test_a_meshtastic_preset_picks_its_region(self):
        """The region drives the slot ranking, so the preset has to carry it."""
        page = _page()

        page._preset.setCurrentIndex(page._preset.findData("meshtastic-eu-868"))

        assert page._region.currentData() == "EU_868"

    def test_a_single_frequency_preset_leaves_the_region_alone(self):
        page = _page()
        before = page._region.currentData()

        page._preset.setCurrentIndex(page._preset.findData("reticulum-us"))

        assert page._region.currentData() == before

    def test_it_marks_the_channels_it_tuned_for(self):
        page = _page()
        markers = default_preset().markers

        # Two plot items per marker: the shaded band and its label.
        assert len(page._waterfall2d._marker_items) == 2 * len(markers)

    def test_a_hand_edit_stops_claiming_the_preset(self):
        page = _page()

        page._center.setValue(920.0)

        assert page._preset.currentData() is None
        assert page._preset.currentText() == "Custom"

    def test_writing_the_controls_is_not_taken_for_a_hand_edit(self):
        """Applying a preset writes those same controls and must survive it."""
        page = _page()

        page._preset.setCurrentIndex(page._preset.findData("meshtastic-us"))

        assert page._preset.currentData() == "meshtastic-us"

    def test_the_combo_explains_the_preset_it_is_on(self):
        page = _page()

        assert "slot" in page._preset.toolTip().lower()


#: Two dongles as this machine reports them: same serial, different products.
_TWO_DONGLES = [
    SdrDevice(0, "RTLSDRBlog", "Blog V4", "00000001"),
    SdrDevice(1, "RTLSDRBlog", "Blog V4L", "00000001"),
]


class TestDongleSelector:
    """Which dongle a view captures from.

    This machine has two attached and they are not equivalent — device 0 reports a
    Blog V4 with the R828D tuner and device 1 a Blog V4L with an R820T — so the
    selection has to reach the command line, not just the label.
    """

    def test_it_starts_on_the_first_dongle_without_touching_hardware(self):
        """Building a page must not enumerate: that runs rtl_test and opens a device."""
        page = _page()

        assert page._selected_device() == 0
        assert page._dongle.count() == 1

    def test_refreshing_lists_what_is_attached(self, monkeypatch):
        page = _page()
        monkeypatch.setattr(
            rtl_tools, "list_devices",
            lambda *a, **k: (_TWO_DONGLES, "2 RTL-SDR dongle(s) found"),
        )

        page._refresh_dongles()

        assert page._dongle.count() == 2
        assert page._dongle.itemText(0) == "0 · Blog V4"
        assert page._dongle.itemText(1) == "1 · Blog V4L"
        assert "2 RTL-SDR" in page._status.text()

    def test_the_labels_lead_with_the_index_because_the_serials_match(self, monkeypatch):
        page = _page()
        monkeypatch.setattr(
            rtl_tools, "list_devices", lambda *a, **k: (_TWO_DONGLES, "2 found")
        )

        page._refresh_dongles()

        assert page._dongle.itemText(0) != page._dongle.itemText(1)
        assert "SN 00000001" in page._dongle.toolTip()

    def test_a_refresh_keeps_the_chosen_dongle(self, monkeypatch):
        page = _page()
        monkeypatch.setattr(
            rtl_tools, "list_devices", lambda *a, **k: (_TWO_DONGLES, "2 found")
        )
        page._refresh_dongles()
        page._dongle.setCurrentIndex(page._dongle.findData(1))

        page._refresh_dongles()

        assert page._selected_device() == 1

    def test_an_absent_dongle_is_not_an_empty_control(self, monkeypatch):
        """An empty combo would leave nothing to select and nothing to explain."""
        page = _page()
        monkeypatch.setattr(
            rtl_tools, "list_devices",
            lambda *a, **k: ([], "No supported devices found. Is a dongle plugged in?"),
        )

        page._refresh_dongles()

        assert page._dongle.count() == 1
        assert page._selected_device() == 0
        assert "plugged in" in page._dongle.toolTip()

    def test_the_capture_goes_to_the_selected_dongle(self, monkeypatch):
        page = _page()
        stub = _StubCapture()
        monkeypatch.setattr(page, "_ensure_capture", lambda: stub)
        page._dongle.addItem("1 · Blog V4L", userData=1)
        page._dongle.setCurrentIndex(page._dongle.findData(1))

        page._on_capture_clicked()

        assert stub.started[0][3] == 1, "the index has to reach the command line"

    def test_the_band_scan_carries_the_selected_dongle(self, monkeypatch):
        page = _page()
        stub = _StubScan()
        monkeypatch.setattr(page, "_ensure_scan", lambda: stub)
        page._dongle.addItem("1 · Blog V4L", userData=1)
        page._dongle.setCurrentIndex(page._dongle.findData(1))

        page._on_scan_clicked()

        assert stub.requests and stub.requests[0].device_index == 1
