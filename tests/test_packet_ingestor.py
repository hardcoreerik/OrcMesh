"""Tests for services.packet_ingestor — no Qt event loop, no hardware."""
from __future__ import annotations

import json
import unittest.mock as mock
from pathlib import Path


# ── Minimal Qt app for QObject signals ───────────────────────────────────────
# PacketIngestor is a QObject; Qt signals require at minimum a QCoreApplication.
import sys
from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.models.network_session import NetworkSession
from meshchat.models.network_packet import NetworkPacket
from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.services.packet_ingestor import PacketIngestor

_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "packets.json").read_text(encoding="utf-8")
)


def _make_ingestor() -> PacketIngestor:
    session = NetworkSession.new(transport="ble", connection_target="AA:BB:CC:DD:EE:FF")
    return PacketIngestor(session, store=None)


# ── Basic ingestion ────────────────────────────────────────────────────────────

class TestIngestRaw:
    def test_ingest_direct_text_increments_session_count(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])
        assert ing._session.packet_count == 1

    def test_ingest_adds_to_recent_packets(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])
        packets = ing.get_recent_packets()
        assert len(packets) == 1
        assert isinstance(packets[0], NetworkPacket)

    def test_ingest_returns_correct_portnum(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])
        pkt = ing.get_recent_packets()[0]
        assert pkt.portnum == 1

    def test_ingest_computes_hops_used(self):
        """direct_text has hopStart=3, hopLimit=3 → hops_used=0."""
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])
        pkt = ing.get_recent_packets()[0]
        assert pkt.hops_used == 0

    def test_ingest_one_hop(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["one_hop_text"])
        assert ing.get_recent_packets()[0].hops_used == 1

    def test_ingest_two_hops(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["multi_hop_text"])
        assert ing.get_recent_packets()[0].hops_used == 2

    def test_ingest_mqtt_packet(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["mqtt_text"])
        pkt = ing.get_recent_packets()[0]
        assert pkt.via_mqtt is True

    def test_ingest_hop_start_zero_unknown_hops(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["unknown_hop_start_zero"])
        pkt = ing.get_recent_packets()[0]
        assert pkt.hops_used is None

    def test_ingest_position_portnum(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["position"])
        pkt = ing.get_recent_packets()[0]
        assert pkt.portnum == 3

    def test_ingest_telemetry_portnum(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["telemetry"])
        pkt = ing.get_recent_packets()[0]
        assert pkt.portnum == 67

    def test_malformed_packet_ignored(self):
        """A packet with decoded=garbage string should not crash; count stays 0."""
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["malformed_decoded_not_dict"])
        assert ing._session.packet_count == 0

    def test_ingest_multiple_packets(self):
        ing = _make_ingestor()
        for key in ("direct_text", "one_hop_text", "multi_hop_text", "mqtt_text"):
            ing.ingest_raw(_FIXTURES[key])
        assert ing._session.packet_count == 4


# ── Deduplication ─────────────────────────────────────────────────────────────

class TestDeduplication:
    def test_same_packet_id_is_deduped(self):
        ing = _make_ingestor()
        pkt = dict(_FIXTURES["direct_text"])  # same id=55001
        ing.ingest_raw(pkt)
        ing.ingest_raw(pkt)
        assert ing._session.packet_count == 1

    def test_different_ids_both_ingested(self):
        ing = _make_ingestor()
        pkt1 = dict(_FIXTURES["direct_text"])
        pkt2 = dict(_FIXTURES["one_hop_text"])
        ing.ingest_raw(pkt1)
        ing.ingest_raw(pkt2)
        assert ing._session.packet_count == 2

    def test_same_id_different_channel_both_ingested(self):
        """Different channel → different dedup key."""
        ing = _make_ingestor()
        pkt1 = dict(_FIXTURES["direct_text"])
        pkt2 = dict(_FIXTURES["direct_text"])
        pkt2 = dict(pkt2, channel=1)
        ing.ingest_raw(pkt1)
        ing.ingest_raw(pkt2)
        assert ing._session.packet_count == 2

    def test_packet_id_zero_is_deduped_by_the_strong_key(self):
        # id == 0 is a legitimate packet id (same node_num == 0 pattern
        # fixed elsewhere) — a truthy check on it would wrongly fall
        # through to the weaker, 5-second-bucketed fallback fingerprint
        # instead of the strong sender:pid:portnum:channel key.
        #
        # Submitting the identical packet twice isn't a real test of this:
        # both the strong key AND the weak fallback fingerprint would dedupe
        # it (same content, same 5s bucket), so it'd pass even with the old
        # truthy check. Vary the decoded content on the second submission —
        # only the strong (sender, id, portnum, channel) key still catches
        # this as a dup; the fallback fingerprint would treat it as new.
        ing = _make_ingestor()
        pkt = dict(_FIXTURES["direct_text"], id=0)
        pkt2 = dict(pkt, decoded={**pkt["decoded"], "text": "different"})
        ing.ingest_raw(pkt)
        ing.ingest_raw(pkt2)
        assert ing._session.packet_count == 1

    def test_anonymous_packet_deduped_by_content_fingerprint(self):
        """
        Packets without 'from'/'id' get a SHA-256 content+time-bucket fingerprint.
        Identical content submitted twice within the same 5-second bucket is deduped.
        """
        ing = _make_ingestor()
        minimal = {"decoded": {"portnum": 1, "text": "anon"}}
        ing.ingest_raw(minimal)
        ing.ingest_raw(minimal)
        # Both calls happen within the same 5-second bucket → same hash → deduped
        assert ing._session.packet_count == 1


# ── Node snapshots ────────────────────────────────────────────────────────────

class TestNodeSnapshot:
    def test_node_created_on_first_packet(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])
        nodes = ing.get_nodes()
        assert len(nodes) == 1
        assert nodes[0].node_num == 111111

    def test_node_packet_count_increments(self):
        ing = _make_ingestor()
        pkt = dict(_FIXTURES["direct_text"])
        # Use a unique id each time to bypass dedup
        pkt2 = dict(pkt, id=pkt["id"] + 1000)
        ing.ingest_raw(pkt)
        ing.ingest_raw(pkt2)
        node = ing.get_node(111111)
        assert node.packet_count == 2

    def test_node_text_count_increments(self):
        ing = _make_ingestor()
        pkt = _FIXTURES["direct_text"]
        pkt2 = dict(pkt, id=pkt["id"] + 1)
        ing.ingest_raw(pkt)
        ing.ingest_raw(pkt2)
        assert ing.get_node(111111).text_count == 2

    def test_node_snr_and_rssi_updated(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])
        node = ing.get_node(111111)
        assert node.last_snr == 12.5
        assert node.last_rssi == -68

    def test_node_hops_used_updated(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])
        assert ing.get_node(111111).last_hops_used == 0

    def test_node_rf_count_incremented_for_non_mqtt(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])
        assert ing.get_node(111111).rf_count == 1
        assert ing.get_node(111111).via_mqtt_count == 0

    def test_node_via_mqtt_count_incremented(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["mqtt_text"])
        assert ing.get_node(444444).via_mqtt_count == 1
        assert ing.get_node(444444).rf_count == 0

    def test_unknown_transport_increments_neither_counter(self):
        # via_mqtt absent entirely (older firmware that never reports it)
        # must not be misclassified as confirmed RF — `pkt.is_via_mqtt`
        # coerces None to False via bool(), which used to do exactly that.
        # These counters now persist across restarts, so a wrong count
        # here would keep compounding every session.
        ing = _make_ingestor()
        pkt = dict(_FIXTURES["direct_text"])
        del pkt["viaMqtt"]
        ing.ingest_raw(pkt)
        node = ing.get_node(111111)
        assert node.rf_count == 0
        assert node.via_mqtt_count == 0

    def test_node_last_via_mqtt_tracks_the_most_recent_packet(self):
        # Feeds NodeSnapshot.is_direct's via_mqtt check — a node's cumulative
        # via_mqtt_count/rf_count can't tell you whether the MOST RECENT
        # packet was bridged, which is what "is this node currently direct"
        # needs.
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["mqtt_text"])
        assert ing.get_node(444444).last_via_mqtt is True

        ing.ingest_raw(_FIXTURES["direct_text"])
        assert ing.get_node(111111).last_via_mqtt is False

    def test_node_name_populated_from_node_info(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["node_info"])
        node = ing.get_node(555555)
        assert node is not None
        assert node.long_name == "Remote Alpha"
        assert node.short_name == "ALPH"

    def test_multiple_senders_tracked_separately(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])   # sender 111111
        ing.ingest_raw(_FIXTURES["one_hop_text"])  # sender 222222
        nodes = ing.get_nodes()
        nums = {n.node_num for n in nodes}
        assert 111111 in nums
        assert 222222 in nums

    def test_get_node_returns_none_for_unknown(self):
        ing = _make_ingestor()
        assert ing.get_node(0xDEAD_BEEF) is None


# ── Seeding from persisted history ─────────────────────────────────────────────

class TestSeedFromStore:
    """seed_from_store() restores nodes from MonitorStore rows at startup.
    last_snr/last_rssi/last_hops_used/last_hop_start/last_via_mqtt and the
    rf_count/via_mqtt_count/position_count/telemetry_count counters used to
    have no DB column at all, so they never made it into these rows and a
    long-tracked node started every restart with them reset."""

    def test_signal_hop_and_counter_fields_are_restored(self):
        ing = _make_ingestor()
        ing.seed_from_store(
            node_rows=[{
                "node_num": 1, "node_id": "!00000001",
                "last_snr": 8.25, "last_rssi": -72,
                "last_hops_used": 0, "last_hop_start": 3, "last_via_mqtt": 0,
                "rf_count": 4, "via_mqtt_count": 1,
                "position_count": 2, "telemetry_count": 1,
            }],
            positions_by_num={},
        )
        node = ing.get_node(1)
        assert node.last_snr == 8.25
        assert node.last_rssi == -72
        assert node.last_hops_used == 0
        assert node.last_hop_start == 3
        assert node.last_via_mqtt is False
        assert node.rf_count == 4
        assert node.via_mqtt_count == 1
        assert node.position_count == 2
        assert node.telemetry_count == 1

    def test_missing_last_via_mqtt_stays_none_not_false(self):
        # A row with the column present but NULL (never observed) must not
        # be conflated with an explicit False (confirmed direct RF).
        ing = _make_ingestor()
        ing.seed_from_store(
            node_rows=[{"node_num": 2, "last_via_mqtt": None}],
            positions_by_num={},
        )
        assert ing.get_node(2).last_via_mqtt is None

    def test_live_packet_before_seeding_is_not_overwritten(self):
        # A node already heard live this session must keep its live data —
        # seeding is startup-only backfill, not an authoritative overwrite.
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])  # creates node 111111
        live_snr = ing.get_node(111111).last_snr
        ing.seed_from_store(
            node_rows=[{"node_num": 111111, "last_snr": -99.0}],
            positions_by_num={},
        )
        assert ing.get_node(111111).last_snr == live_snr


# ── Signals emitted ───────────────────────────────────────────────────────────

class TestSignals:
    def test_packet_ingested_signal_emitted(self):
        ing = _make_ingestor()
        received = []
        ing.packet_ingested.connect(received.append)
        ing.ingest_raw(_FIXTURES["direct_text"])
        assert len(received) == 1
        assert isinstance(received[0], NetworkPacket)


# ── Real wire shape (what the meshtastic library actually publishes) ──────────

class TestRealisticWireShape:
    """meshtastic builds its packet dicts with protobuf's MessageToDict, so
    ``decoded.portnum`` arrives as an enum *name* ("POSITION_APP"), while
    every comparison in the ingestor is against the int constants in
    ``analytics.packet_classifier``. Nothing used to normalize that, so no
    packet ever matched ``PORTNUM_POSITION``/``PORTNUM_TELEMETRY``: position
    packets were never processed (no live map updates, no ``positions``
    rows), telemetry was dropped the same way, and every packet was labelled
    "Other Known (…)". The ``wire_*`` fixtures carry the real shape, and
    FakeMeshtasticInterface now emits it too — an int-only fake is what let
    this go unnoticed in the first place.
    """

    def test_enum_name_text_portnum_is_normalized_to_its_int(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["wire_text"])
        pkt = ing.get_recent_packets()[0]
        assert pkt.portnum == 1
        assert pkt.portnum_name == "Text"
        assert ing.get_node(101010).text_count == 1

    def test_enum_name_position_packet_reaches_the_map(self):
        ing = _make_ingestor()
        positions = []
        ing.position_updated.connect(positions.append)
        ing.ingest_raw(_FIXTURES["wire_position"])

        assert len(positions) == 1, "live position packet was not processed"
        sample = positions[0]
        assert sample.node_num == 202020
        assert sample.latitude == 52.0
        assert sample.longitude == 13.0
        assert ing.get_node(202020).position_count == 1

    def test_zero_ground_speed_and_track_are_preserved(self):
        # An explicitly-reported 0 is a real reading (stationary node, due
        # north) — `a or b` discarded it in favour of the absent snake_case
        # key's None.
        ing = _make_ingestor()
        positions = []
        ing.position_updated.connect(positions.append)
        ing.ingest_raw(_FIXTURES["wire_position"])
        sample = positions[0]
        assert sample.speed_ms == 0.0
        assert sample.heading_deg == 0.0

    def test_position_time_is_taken_from_the_payload(self):
        ing = _make_ingestor()
        positions = []
        ing.position_updated.connect(positions.append)
        ing.ingest_raw(_FIXTURES["wire_position"])
        sample = positions[0]
        assert sample.position_time is not None, "positions.position_time was always NULL before"
        assert int(sample.position_time.timestamp()) == 1780000000
        assert sample.precision_bits == 16

    def test_zero_precision_bits_is_carried_through_not_dropped(self):
        ing = _make_ingestor()
        positions = []
        ing.position_updated.connect(positions.append)
        pkt = dict(_FIXTURES["wire_position"])
        pkt["decoded"] = {
            **pkt["decoded"],
            "position": {**pkt["decoded"]["position"], "precisionBits": 0},
        }
        ing.ingest_raw(pkt)
        assert positions[0].precision_bits == 0

    def test_enum_name_telemetry_packet_is_processed(self):
        ing = _make_ingestor()
        samples = []
        ing.telemetry_updated.connect(samples.append)
        ing.ingest_raw(_FIXTURES["wire_telemetry"])

        assert len(samples) == 1, "live telemetry packet was not processed"
        assert samples[0].battery_level == 87.0
        node = ing.get_node(202020)
        assert node.telemetry_count == 1
        # Merged onto the snapshot for the Node Inspector, which reads the
        # node rather than the transient signal.
        assert node.battery_level == 87.0

    def test_unknown_enum_name_is_reported_as_unknown_port(self):
        ing = _make_ingestor()
        ing.ingest_raw({
            "from": 1, "id": 55090,
            "decoded": {"portnum": "SOME_FUTURE_APP"},
        })
        pkt = ing.get_recent_packets()[0]
        assert pkt.portnum is None
        assert pkt.portnum_name == "Unknown Port"

    def test_numeric_string_portnum_is_accepted(self):
        # Not a shape the library emits, but a JSON round-trip of a packet
        # can produce it, and it must not be classified as unknown.
        ing = _make_ingestor()
        ing.ingest_raw({"from": 1, "id": 55091, "decoded": {"portnum": "3"}})
        assert ing.get_recent_packets()[0].portnum == 3

    def test_name_and_number_shapes_dedupe_to_the_same_key(self):
        ing = _make_ingestor()
        ing.ingest_raw({"from": 5, "id": 55092, "decoded": {"portnum": "POSITION_APP"}})
        ing.ingest_raw({"from": 5, "id": 55092, "decoded": {"portnum": 3}})
        assert ing._session.packet_count == 1

    def test_fake_interface_packet_shape_is_understood(self):
        """Guards the fake as well as the ingestor: if the fake drifts back
        to bare int portnums, it stops exercising the normalization path
        every real radio goes through."""
        from tests.fakes.fake_meshtastic_interface import FakeMeshtasticInterface

        fake = FakeMeshtasticInterface()
        fake.close()  # cancels the fake's scheduled connection.established event
        with mock.patch("tests.fakes.fake_meshtastic_interface._pub.sendMessage") as send:
            fake.pump_text("from the fake")
        raw = send.call_args_list[-1].kwargs["packet"]
        assert isinstance(raw["decoded"]["portnum"], str), "fake no longer matches the wire shape"

        ing = _make_ingestor()
        ing.ingest_raw(raw)
        assert ing.get_recent_packets()[0].portnum == 1

    def test_node_updated_signal_emitted(self):
        ing = _make_ingestor()
        received = []
        ing.node_updated.connect(received.append)
        ing.ingest_raw(_FIXTURES["direct_text"])
        assert len(received) == 1
        assert isinstance(received[0], NodeSnapshot)

    def test_stats_updated_signal_emitted(self):
        ing = _make_ingestor()
        stats = []
        ing.stats_updated.connect(lambda pm, ph: stats.append((pm, ph)))
        ing.ingest_raw(_FIXTURES["direct_text"])
        assert len(stats) == 1
        pm, ph = stats[0]
        assert pm >= 1
        assert ph >= 1


# ── Thread safety of the packet ring ──────────────────────────────────────────
# The worker thread appends while the GUI thread snapshots for CSV export.
#
# Honest caveat: on CPython with the GIL, `list(deque)` takes a C-level fast
# path and does NOT reliably raise, so this is a smoke test, not a test with
# teeth — it passes with or without the lock. The lock is still correct:
# that fast path is an unguaranteed implementation detail, Python-level
# iteration over the same deque *does* raise "deque mutated during iteration",
# and free-threaded builds remove the GIL this relies on.

class TestRecentPacketsThreadSafety:
    def test_snapshot_while_appending_does_not_raise(self):
        import threading

        ing = _make_ingestor()
        errors: list[BaseException] = []
        stop = threading.Event()

        def writer():
            try:
                i = 0
                while not stop.is_set():
                    pkt = dict(_FIXTURES["direct_text"])
                    pkt["id"] = i  # unique id so dedup never suppresses it
                    i += 1
                    ing.ingest_raw(pkt)
            except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
                errors.append(exc)

        def reader():
            try:
                for _ in range(3000):
                    ing.get_recent_packets()
            except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
                errors.append(exc)

        w = threading.Thread(target=writer, daemon=True)
        r = threading.Thread(target=reader, daemon=True)
        w.start()
        r.start()
        r.join(timeout=30)
        stop.set()
        w.join(timeout=30)

        assert not errors, f"concurrent access raised: {errors[0]!r}"

    def test_snapshot_is_a_copy_not_a_live_view(self):
        ing = _make_ingestor()
        ing.ingest_raw(dict(_FIXTURES["direct_text"], id=1))
        snapshot = ing.get_recent_packets()
        before = len(snapshot)
        ing.ingest_raw(dict(_FIXTURES["direct_text"], id=2))
        assert len(snapshot) == before, "snapshot must not reflect later appends"

    def test_ring_is_bounded(self):
        ing = _make_ingestor()
        assert ing._recent_packets.maxlen == ing._recent_max
