"""Tests for services.packet_ingestor — no Qt event loop, no hardware."""
from __future__ import annotations

import json
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

    def test_update_node_from_db(self):
        ing = _make_ingestor()
        ing.ingest_raw(_FIXTURES["direct_text"])  # creates node 111111
        db = {
            111111: {
                "user": {
                    "id": "!0001b1c7",
                    "longName": "From DB",
                    "shortName": "DB",
                }
            }
        }
        ing.update_node_from_db(111111, db)
        assert ing.get_node(111111).long_name == "From DB"


# ── Signals emitted ───────────────────────────────────────────────────────────

class TestSignals:
    def test_packet_ingested_signal_emitted(self):
        ing = _make_ingestor()
        received = []
        ing.packet_ingested.connect(received.append)
        ing.ingest_raw(_FIXTURES["direct_text"])
        assert len(received) == 1
        assert isinstance(received[0], NetworkPacket)

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
