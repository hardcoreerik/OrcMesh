"""Tests for the RF device registry.

The IIO listing below is verbatim output from ``iio_info -s`` on this bench with one
PlutoSDR clone attached, because the whole point of the registry is a shape a first look
gets wrong: three contexts, one board. The RTL listing is the same real text
``test_sdr_source`` uses — two dongles reporting the identical serial.
"""
from __future__ import annotations

from meshchat.services import rf, rtl_tools
from meshchat.services.rf import base, registry

#: Verbatim ``iio_info -s``: one clone reachable over Ethernet, over the USB gadget and
#: over libiio's raw USB backend, all at once. Line 1 is the warning the tool writes on
#: Windows before the list, not a context.
_REAL_CONTEXTS = """\
Unable to create Local IIO context : Function not implemented (40)
Available contexts:
        0: 192.168.1.50 (FISH Ball PlutoSDR Rev.A (Z7020-AD9361)), serial=b0d85d89da56de55b2ee997b00499360 [ip:pluto.local]
        1: 192.168.2.1 (FISH Ball PlutoSDR Rev.A (Z7020-AD9361)), serial=b0d85d89da56de55b2ee997b00499360 [ip:pluto.local]
        2: 0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial=b0d85d89da56de55b2ee997b00499360 [usb:1.66.5]
"""

_NOT_FOUND = """\
Unable to create Local IIO context : Function not implemented (40)
No IIO context found.
"""

#: Verbatim ``rtl_test -t``, the same text ``test_sdr_source`` uses. Both dongles carry
#: ``SN: 00000001``, the value the rtl-sdr blog EEPROM ships with.
_REAL_DONGLES = """Found 2 device(s):
  0:  RTLSDRBlog, Blog V4, SN: 00000001
  1:  RTLSDRBlog, Blog V4L, SN: 00000001
"""


class TestTransportKind:
    def test_the_two_ip_forms_are_told_apart(self):
        """They are indistinguishable in a URI list and 3x apart in measured throughput."""
        assert base.transport_kind("ip:192.168.1.50") == base.ETHERNET
        assert base.transport_kind("ip:192.168.2.1") == base.USB_GADGET

    def test_a_usb_uri_is_usb(self):
        assert base.transport_kind("usb:1.66.5") == base.USB

    def test_reliability_outranks_throughput_in_the_preference_order(self):
        """Raw usb: is quicker in its one good run, and dropped the board off the bus twice."""
        assert base.PREFERENCE[base.ETHERNET] < base.PREFERENCE[base.USB_GADGET]
        assert base.PREFERENCE[base.USB_GADGET] < base.PREFERENCE[base.USB]


class TestContextParsing:
    def test_the_real_listing_parses_into_three_contexts(self):
        contexts = registry.parse_contexts(_REAL_CONTEXTS)

        assert [context.index for context in contexts] == [0, 1, 2]
        assert contexts[0].address == "192.168.1.50"
        assert contexts[2].uri == "usb:1.66.5"

    def test_the_windows_warning_is_not_a_context(self):
        contexts = registry.parse_contexts(_REAL_CONTEXTS)

        assert len(contexts) == 3
        assert not any("Unable to create" in context.model for context in contexts)

    def test_no_context_found_is_an_empty_list_not_an_error(self):
        assert registry.parse_contexts(_NOT_FOUND) == []

    def test_a_context_reporting_no_serial_is_listed_not_dropped(self):
        devices = registry.pluto_devices(
            registry.parse_contexts("        0: 192.168.1.50 (Some Radio), serial= [ip:192.168.1.50]\n")
        )

        assert len(devices) == 1
        assert devices[0].key == "pluto:noserial:192.168.1.50"

    def test_two_boards_without_serials_are_not_merged(self):
        devices = registry.pluto_devices(
            registry.parse_contexts(
                "        0: 192.168.1.50 (Some Radio), serial= [ip:192.168.1.50]\n"
                "        1: 192.168.1.51 (Some Radio), serial= [ip:192.168.1.51]\n"
            )
        )

        assert len(devices) == 2


class TestIioContextUris:
    def test_both_ip_contexts_report_the_same_discovery_name(self):
        """A trap in the real data: the URI alone cannot tell LAN from USB gadget.

        Verbatim, contexts 0 and 1 both say ``[ip:pluto.local]``. Keying a transport on
        that would collapse the fast path and the slow one into a single entry, so the
        address is used as the handle instead.
        """
        contexts = registry.parse_contexts(_REAL_CONTEXTS)

        assert contexts[0].uri == contexts[1].uri == "ip:pluto.local"
        assert contexts[0].transport_uri != contexts[1].transport_uri

    def test_the_usb_context_keeps_its_uri_because_its_address_is_not_one(self):
        """``0456:b673`` is a VID:PID; ``usb:1.66.5`` is the handle that works."""
        contexts = registry.parse_contexts(_REAL_CONTEXTS)

        assert contexts[2].transport_uri == "usb:1.66.5"

    def test_a_context_is_classified_by_the_handle_not_the_discovery_name(self):
        """Otherwise the gadget would be ranked as Ethernet and win the preference."""
        contexts = registry.parse_contexts(_REAL_CONTEXTS)

        assert contexts[0].kind == base.ETHERNET
        assert contexts[1].kind == base.USB_GADGET
        assert contexts[2].kind == base.USB


class TestOneBoardManyTransports:
    def test_three_contexts_become_one_device(self):
        devices = registry.pluto_devices(registry.parse_contexts(_REAL_CONTEXTS))

        assert len(devices) == 1

    def test_every_uri_is_kept_as_a_transport(self):
        device = registry.pluto_devices(registry.parse_contexts(_REAL_CONTEXTS))[0]

        assert set(device.uris) == {"ip:192.168.1.50", "ip:192.168.2.1", "usb:1.66.5"}

    def test_the_identity_is_the_serial_rather_than_a_uri(self):
        device = registry.pluto_devices(registry.parse_contexts(_REAL_CONTEXTS))[0]

        assert device.key == "pluto:b0d85d89da56de55b2ee997b00499360"

    def test_the_ethernet_transport_is_the_one_chosen_for_streaming(self):
        """It is the measured fast path: 15 MSPS lossless against the gadget's 5."""
        device = registry.pluto_devices(registry.parse_contexts(_REAL_CONTEXTS))[0]
        chosen = device.streaming_transport()

        assert chosen is not None
        assert chosen.uri == "ip:192.168.1.50"

    def test_the_usb_name_does_not_become_the_device_name(self):
        """Over USB the clone's descriptors claim to be an ADALM-PLUTO. It is not one."""
        device = registry.pluto_devices(registry.parse_contexts(_REAL_CONTEXTS))[0]

        assert "Analog Devices" not in device.label
        assert "FISH Ball" in device.label

    def test_the_disagreement_is_recorded_rather_than_quietly_resolved(self):
        device = registry.pluto_devices(registry.parse_contexts(_REAL_CONTEXTS))[0]

        assert any("ADALM-PLUTO" in note for note in device.notes)

    def test_it_says_only_one_transport_can_hold_the_tuner(self):
        device = registry.pluto_devices(registry.parse_contexts(_REAL_CONTEXTS))[0]

        assert any("tuner" in note for note in device.notes)

    def test_a_single_transport_raises_no_such_warning(self):
        device = registry.pluto_devices(
            registry.parse_contexts("        0: 192.168.1.50 (Some Radio), serial=AAA [ip:192.168.1.50]\n")
        )[0]

        assert not any("tuner" in note for note in device.notes)


class TestRtlDevices:
    def test_dongles_sharing_a_serial_stay_two_devices(self):
        devices = registry.rtl_devices(rtl_tools.parse_device_list(_REAL_DONGLES))

        assert len(devices) == 2
        assert [device.key for device in devices] == ["rtl:0", "rtl:1"]

    def test_a_shared_serial_is_admitted_in_a_note(self):
        devices = registry.rtl_devices(rtl_tools.parse_device_list(_REAL_DONGLES))

        assert all(any("bus index" in note for note in device.notes) for device in devices)

    def test_distinct_serials_need_no_such_note(self):
        devices = registry.rtl_devices(
            rtl_tools.parse_device_list(
                "Found 2 device(s):\n"
                "  0:  RTLSDRBlog, Blog V4, SN: 00000001\n"
                "  1:  RTLSDRBlog, Blog V4L, SN: 00000002\n"
            )
        )

        assert all(not device.notes for device in devices)

    def test_a_dongle_has_no_uri_because_it_is_addresssed_by_index(self):
        device = registry.rtl_devices(rtl_tools.parse_device_list(_REAL_DONGLES))[0]

        assert device.uris == ()
        assert device.streaming_transport() is None

    def test_the_label_leads_with_the_index(self):
        """It is the only discriminator these dongles have."""
        device = registry.rtl_devices(rtl_tools.parse_device_list(_REAL_DONGLES))[0]

        assert device.label.startswith("0 ·")


class TestDiscovery:
    def test_it_merges_both_kinds_of_radio(self, monkeypatch):
        monkeypatch.setattr(
            rtl_tools, "list_devices",
            lambda timeout_s=8.0: (rtl_tools.parse_device_list(_REAL_DONGLES), "2 dongles"),
        )
        monkeypatch.setattr(registry, "_scan_iio_contexts", lambda timeout_s: _REAL_CONTEXTS)

        devices, problems = registry.discover()

        # Two dongles plus one board -- three devices, not the four a URI-keyed count gives.
        assert len(devices) == 3
        assert problems == ()

    def test_a_missing_iio_tool_is_reported_rather_than_raised(self, monkeypatch):
        monkeypatch.setattr(
            rtl_tools, "list_devices",
            lambda timeout_s=8.0: (rtl_tools.parse_device_list(_REAL_DONGLES), "2 dongles"),
        )
        monkeypatch.setattr(registry, "_scan_iio_contexts", lambda timeout_s: None)

        devices, problems = registry.discover()

        assert len(devices) == 2
        assert any("iio_info" in problem for problem in problems)

    def test_no_dongles_still_says_why(self, monkeypatch):
        monkeypatch.setattr(
            rtl_tools, "list_devices",
            lambda timeout_s=8.0: ([], "rtl_test was not found, so the dongles cannot be listed."),
        )
        monkeypatch.setattr(registry, "_scan_iio_contexts", lambda timeout_s: _REAL_CONTEXTS)

        devices, problems = registry.discover()

        assert len(devices) == 1
        assert any("rtl_test" in problem for problem in problems)

    def test_discovery_is_re_exported_from_the_package(self):
        """Callers should not have to know which module inside rf owns it."""
        assert rf.discover is registry.discover
