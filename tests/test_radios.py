"""Tests for radio identity, enumeration, and the per-radio lease.

The fixtures are the real port metadata measured on this bench on 2026-09-27, verbatim,
because the grouping rules here were derived from that data and a tidy invented example
would not have caught the thing that makes this module necessary: the Heltec's serial
number and its Bluetooth address differ by one, so they are two doors onto ONE radio.
"""
from __future__ import annotations

import pytest

from meshchat.services import radios
from meshchat.services.radios import base, lease


@pytest.fixture(autouse=True)
def _empty_leases():
    """The lease is process-wide, so no test may inherit another's holdings."""
    with lease._radio_lock:
        lease._radio_owners.clear()
    yield
    with lease._radio_lock:
        lease._radio_owners.clear()


# ── Real hardware, verbatim from serial.tools.list_ports ────────────────────────
HELTEC = radios.PortSummary(
    device="COM24",
    description="USB Serial Device (COM24)",
    hwid="USB VID:PID=303A:1001 SER=44:1B:F6:6F:81:BC",
    serial_number="44:1B:F6:6F:81:BC",
    vid=0x303A,
    pid=0x1001,
)
TBEAM = radios.PortSummary(
    device="COM16",
    description="USB Serial Device (COM16)",
    hwid="USB VID:PID=303A:1001 SER=48CA435BAA2C",
    serial_number="48CA435BAA2C",
    vid=0x303A,
    pid=0x1001,
)
DEV_BOARD = radios.PortSummary(
    device="COM17",
    description="USB Serial Device (COM17)",
    hwid="USB VID:PID=303A:1001 SER=30:ED:A0:E2:E6:95",
    serial_number="30:ED:A0:E2:E6:95",
    vid=0x303A,
    pid=0x1001,
)
BLUETOOTH_LINK = radios.PortSummary(
    device="COM5",
    description="Standard Serial over Bluetooth link (COM5)",
    hwid="BTHENUM\\{00001101-0000-1000-8000-00805F9B34FB}_VID&000105D6_PID&000A\\B&FC6FCB0&0&927CDC7024F9_C00000000",
)

#: The Heltec, over Bluetooth. Note the last octet: BC on serial, BD here.
HELTEC_BLE = radios.BleAdvertisement(address="44:1B:F6:6F:81:BD", name="hrdc_81bc", rssi=-46)


class TestMacFamily:
    def test_a_serial_mac_and_its_neighbouring_bluetooth_address_are_one_board(self):
        """The measurement this whole module exists for.

        The Heltec reports 44:1B:F6:6F:81:BC over serial and advertises 44:1B:F6:6F:81:BD
        over Bluetooth. The OS refuses a second open of COM24 and knows nothing about the
        Bluetooth door, so without this normalisation the same radio can be driven twice.
        """
        assert base.mac_family("44:1B:F6:6F:81:BC") == base.mac_family("44:1B:F6:6F:81:BD")

    def test_different_boards_do_not_collide(self):
        assert base.mac_family("44:1B:F6:6F:81:BC") != base.mac_family("30:ED:A0:E2:E6:95")

    def test_adjacent_boards_do_not_collide(self):
        """MACs are handed out in sequence, so neighbours must stay distinct."""
        assert base.mac_family("44:1B:F6:6F:81:BE") != base.mac_family("44:1B:F6:6F:81:BC")

    def test_case_and_whitespace_do_not_matter(self):
        assert base.mac_family(" 44:1b:f6:6f:81:bd ") == base.mac_family("44:1B:F6:6F:81:BC")

    def test_the_tbeam_reports_the_same_mac_without_separators(self):
        """Real hardware, and the case this got wrong at first.

        The T-Beam's serial number is 48CA435BAA2C and its Bluetooth address is
        48:CA:43:5B:AA:2D -- the same MAC in a different notation, offset by one exactly
        like the Heltec's. An earlier version of this module only accepted the
        colon-separated form, so the T-Beam was listed twice and told it had no hardware
        address when it plainly did. This test previously asserted that wrong answer.
        """
        assert base.mac_family("48CA435BAA2C") == base.mac_family("48:CA:43:5B:AA:2D")
        assert base.mac_family("48CA435BAA2C") == "48:CA:43:5B:AA:2C"

    def test_both_bench_radios_follow_the_same_offset_convention(self):
        """Two boards, same +1, which is what makes it a convention and not a fluke."""
        assert base.mac_family("44:1B:F6:6F:81:BC") == base.mac_family("44:1B:F6:6F:81:BD")
        assert base.mac_family("48CA435BAA2C") == base.mac_family("48:CA:43:5B:AA:2D")

    def test_the_two_radios_do_not_group_with_each_other(self):
        assert base.mac_family("48CA435BAA2C") != base.mac_family("44:1B:F6:6F:81:BC")

    def test_hyphens_are_accepted_too(self):
        assert base.mac_family("48-CA-43-5B-AA-2D") == "48:CA:43:5B:AA:2C"

    def test_a_bare_hex_serial_that_is_not_twelve_digits_is_not_an_address(self):
        assert base.mac_family("48CA435BAA") is None
        assert base.mac_family("48CA435BAA2CFF") is None

    def test_a_port_name_is_not_an_address(self):
        """COM24 looks nothing like a MAC, and must not be forced into looking like one."""
        assert base.mac_family("COM24") is None

    def test_a_mac_family_of_nothing_is_none_not_an_empty_string(self):
        assert base.mac_family("") is None
        assert base.mac_family("COM24") is None


class TestCandidateKey:
    def test_a_node_number_wins_over_an_address(self):
        """The mesh's own name for the radio, agreed by every node that hears it."""
        assert base.candidate_key(node_num=2859752693, address="COM24") == "radio:2859752693"

    def test_an_address_is_used_when_that_is_all_there_is(self):
        assert base.candidate_key(address="44:1B:F6:6F:81:BC") == "radio:44:1B:F6:6F:81:BC"

    def test_two_doors_produce_one_key(self):
        """The property the lease depends on: same radio, one key, either transport."""
        serial_key = base.candidate_key(address=HELTEC.serial_number)
        ble_key = base.candidate_key(address=HELTEC_BLE.address)

        assert serial_key == ble_key

    def test_a_port_that_is_not_an_address_still_gets_a_key(self):
        assert base.candidate_key(address="COM16") == "radio:COM16"

    def test_nothing_known_gives_an_honest_placeholder(self):
        assert base.candidate_key() == "radio:unknown"


class TestTransportPreference:
    def test_serial_leads(self):
        assert base.PREFERENCE[base.SERIAL] < base.PREFERENCE[base.BLE]

    def test_an_unknown_transport_ranks_last(self):
        assert base.transport_rank("carrier-pigeon") == base.UNRANKED
        assert base.transport_rank("carrier-pigeon") > base.PREFERENCE[base.BLE]

    def test_tcp_is_not_ranked_on_hope(self):
        """It has never been tested here, so it must not be presented as the best path."""
        assert base.PREFERENCE[base.TCP] > base.PREFERENCE[base.SERIAL]


class TestEnumeration:
    def test_the_heltec_appears_once_with_both_doors(self):
        found = radios.candidates(
            ports=[HELTEC, BLUETOOTH_LINK], ads=[HELTEC_BLE],
        )

        assert len(found) == 1, "one radio, not a radio per transport"
        kinds = sorted(t.kind for t in found[0].transports)
        assert kinds == [base.BLE, base.SERIAL]

    def test_serial_is_offered_first_when_both_are_available(self):
        found = radios.candidates(ports=[HELTEC], ads=[HELTEC_BLE])

        assert found[0].preferred().kind == base.SERIAL

    def test_bluetooth_serial_links_are_not_radios(self):
        """Windows exposes every paired Bluetooth profile as a COM port."""
        found = radios.candidates(ports=[BLUETOOTH_LINK])

        assert found == []

    def test_two_radios_are_two_entries(self):
        found = radios.candidates(ports=[HELTEC, TBEAM])

        assert len(found) == 2
        assert {c.preferred().address for c in found} == {"COM24", "COM16"}

    def test_both_bench_radios_group_across_their_two_doors(self):
        """The full real bench: two radios, each reachable over cable and over Bluetooth.

        The T-Beam's Bluetooth address is 48:CA:43:5B:AA:2D against its serial number
        48CA435BAA2C, so it groups for the same reason the Heltec does -- and an earlier
        version of this module failed to group it, listing one radio twice.
        """
        tbeam_ble = radios.BleAdvertisement(address="48:CA:43:5B:AA:2D", name="HcMe_aa2c", rssi=-63)

        found = radios.candidates(ports=[HELTEC, TBEAM], ads=[HELTEC_BLE, tbeam_ble])

        assert len(found) == 2, "two radios, not one entry per door"
        by_key = {c.key: c for c in found}
        assert len(by_key["radio:44:1B:F6:6F:81:BC"].transports) == 2
        assert len(by_key["radio:48:CA:43:5B:AA:2C"].transports) == 2

    def test_a_radio_with_a_real_address_is_not_given_the_unmatched_note(self):
        found = radios.candidates(ports=[TBEAM])

        assert not found[0].notes, "48CA435BAA2C is a hardware address and matches fine"

    def test_a_serial_number_that_is_not_an_address_still_gets_the_note(self):
        """Honest about what is actually unknown, rather than about what looks tidy."""
        odd = radios.PortSummary(
            device="COM9", description="USB Serial Device", hwid="USB VID:PID=303A:1001",
            serial_number="SN23456789", vid=0x303A, pid=0x1001,
        )

        found = radios.candidates(ports=[odd])

        assert "not a hardware address" in " ".join(found[0].notes)

    def test_no_ports_and_no_advertisements_is_an_empty_list_not_an_error(self):
        assert radios.candidates() == []
        assert radios.candidates(ports=[], ads=[]) == []

    def test_a_sorted_list_puts_reachable_radios_first(self):
        found = radios.candidates(ports=[HELTEC, TBEAM])

        assert all(c.preferred() is not None for c in found)


class TestSerialPortAdapter:
    def test_adapting_real_comports_metadata_keeps_the_serial_number(self):
        class _ComPort:
            device = "COM24"
            description = "USB Serial Device (COM24)"
            hwid = "USB VID:PID=303A:1001 SER=44:1B:F6:6F:81:BC"
            serial_number = "44:1B:F6:6F:81:BC"
            vid = 0x303A
            pid = 0x1001

        [summary] = radios.ports_from_comports([_ComPort()])

        assert summary.device == "COM24"
        assert summary.serial_number == "44:1B:F6:6F:81:BC"
        assert summary.looks_like_a_radio

    def test_a_port_with_no_vid_is_offered_rather_than_hidden(self):
        """A radio behind an unusual USB bridge is still a radio."""
        class _MysteryPort:
            device = "COM9"
            description = "USB Serial"
            hwid = "USB\\VID_1234"
            serial_number = None
            vid = None
            pid = None

        [summary] = radios.ports_from_comports([_MysteryPort()])

        assert summary.looks_like_a_radio


class TestLease:
    def test_two_different_radios_can_be_held_at_once(self):
        """The point of the exercise: a second radio is not a conflict."""
        first, _ = radios.acquire_radio("session", "the device page", "radio:2859752693")
        second, _ = radios.acquire_radio("session", "the device page", "radio:1130080812")

        assert first and second
        assert len(radios.held_radios()) == 2

    def test_the_same_radio_cannot_be_held_twice(self):
        radios.acquire_radio("device-page", "the device page", "radio:2859752693")

        ok, complaint = radios.acquire_radio("sigint", "the SIGINT view", "radio:2859752693")

        assert not ok
        assert "already in use by the device page" in complaint

    def test_the_same_radio_over_bluetooth_is_still_the_same_radio(self):
        """The reason the lease is keyed on the radio and not on the port.

        Taken through the registry, because that is where the key comes from: it uses the
        port's hardware address, not the port's name, which is the only reason a cable
        holder and a Bluetooth holder collide at all. A hand-rolled key of "COM24" would
        not, and that is the trap below.
        """
        [by_cable] = radios.candidates(ports=[HELTEC])
        [by_air] = radios.candidates(ads=[HELTEC_BLE])
        assert by_cable.key == by_air.key, "two doors, one radio"

        radios.acquire_radio("cable", "the device page", by_cable.key)

        ok, _ = radios.acquire_radio("wireless", "the SIGINT view", by_air.key)

        assert not ok, "Bluetooth reaches the same board the cable does"

    def test_a_port_with_no_hardware_address_cannot_be_protected_across_transports(self):
        """A documented limitation, not a bug to be discovered later.

        When a port reports no hardware address there is nothing to match it against, so
        the radio is keyed by port name and the Bluetooth door is a different key. Two
        sessions could then drive one board. Nothing here can fix that — it needs the
        node number, which only comes from connecting.
        """
        anonymous = radios.PortSummary(
            device="COM99", description="USB Serial Device", hwid="USB VID:PID=303A:1001",
            serial_number=None, vid=0x303A, pid=0x1001,
        )
        [by_cable] = radios.candidates(ports=[anonymous])
        [by_air] = radios.candidates(ads=[HELTEC_BLE])

        assert by_cable.key != by_air.key, "no address to match on, so no protection"

        radios.acquire_radio("cable", "the device page", by_cable.key)
        ok, _ = radios.acquire_radio("wireless", "the SIGINT view", by_air.key)

        assert ok, "and this is exactly why identification has to come from the radio"

    def test_reacquiring_as_the_same_owner_succeeds(self):
        radios.acquire_radio("me", "the device page", "radio:1")

        ok, _ = radios.acquire_radio("me", "the device page", "radio:1")

        assert ok

    def test_releasing_someone_else_s_lease_does_nothing(self):
        radios.acquire_radio("owner", "the device page", "radio:1")

        radios.release_radio("intruder", "radio:1")

        assert radios.radio_owner("radio:1") == "the device page"

    def test_releasing_returns_the_radio(self):
        radios.acquire_radio("owner", "the device page", "radio:1")

        radios.release_radio("owner", "radio:1")

        assert radios.radio_owner("radio:1") == ""
        ok, _ = radios.acquire_radio("someone-else", "the SIGINT view", "radio:1")
        assert ok

    def test_one_owner_can_hold_several_radios(self):
        radios.acquire_radio("session", "the device page", "radio:1")
        radios.acquire_radio("session", "the device page", "radio:2")

        assert sorted(radios.owner_radios("session")) == ["radio:1", "radio:2"]

    def test_the_complaint_does_not_leak_the_internal_key_prefix(self):
        radios.acquire_radio("a", "the device page", "radio:2859752693")

        _, complaint = radios.acquire_radio("b", "the SIGINT view", "radio:2859752693")

        assert "radio:2859752693" not in complaint
        assert "2859752693" in complaint


class TestHoldContextManager:
    """A lease that outlives a failed connect is the bug this prevents.

    The SDR side had exactly that bug and it had to be fixed by hand at each call site.
    """

    def test_the_radio_is_released_on_a_clean_exit(self):
        with radios.hold("session", "the device page", "radio:1"):
            assert radios.radio_owner("radio:1") == "the device page"

        assert radios.radio_owner("radio:1") == ""

    def test_the_radio_is_released_when_the_block_raises(self):
        """Connecting is the thing most likely to raise, and it happens inside."""
        with pytest.raises(ConnectionError):
            with radios.hold("session", "the device page", "radio:1"):
                raise ConnectionError("the radio stopped answering")

        assert radios.radio_owner("radio:1") == "", (
            "a failed connect must not leave the radio reading as busy forever"
        )

    def test_a_held_radio_refuses_before_the_block_runs(self):
        radios.acquire_radio("first", "the device page", "radio:1")

        ran = False
        with pytest.raises(radios.RadioBusy):
            with radios.hold("second", "the SIGINT view", "radio:1"):
                ran = True  # pragma: no cover - the point is that it never gets here

        assert not ran

    def test_a_refused_hold_does_not_disturb_the_existing_lease(self):
        radios.acquire_radio("first", "the device page", "radio:1")

        with pytest.raises(radios.RadioBusy):
            with radios.hold("second", "the SIGINT view", "radio:1"):
                pass  # pragma: no cover

        assert radios.radio_owner("radio:1") == "the device page"

    def test_two_radios_can_be_held_in_nested_blocks(self):
        with radios.hold("session", "the device page", "radio:1"):
            with radios.hold("session", "the device page", "radio:2"):
                assert len(radios.held_radios()) == 2

        assert radios.held_radios() == {}
