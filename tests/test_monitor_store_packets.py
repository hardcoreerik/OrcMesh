"""Tests for MonitorStore.read_packets_as_objects().

Backs "Export Packet Log" for a full session's history instead of just
PacketIngestor's bounded 10,000-packet in-memory ring buffer — the packets
table has no text column (message content lives in `messages`), so text
always comes back None here; callers that need it merge it back in from
whatever packets are still in memory.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from meshchat.models.network_packet import NetworkPacket
from meshchat.services.monitor_store import MonitorStore


@pytest.fixture
def store(tmp_path):
    s = MonitorStore(db_path=tmp_path / "test.db")
    yield s
    s.shutdown()


def _pkt(**overrides) -> NetworkPacket:
    defaults = dict(
        session_id="sess-1",
        observed_at=datetime.now(timezone.utc),
        rx_time=None,
        sender_num=111111,
        sender_id="!0001b1c7",
        destination_num=None,
        packet_id=42,
        channel_index=0,
        portnum=1,
        portnum_name="TEXT_MESSAGE_APP",
        text="hello",
        payload_size=5,
        rx_snr=8.25,
        rx_rssi=-72,
        hop_start=3,
        hop_limit=3,
        hops_used=0,
        via_mqtt=False,
        transport_mechanism=None,
        pki_encrypted=False,
        want_ack=True,
        priority="DEFAULT",
        raw_metadata_json="{}",
    )
    defaults.update(overrides)
    return NetworkPacket(**defaults)


def _flush(store: MonitorStore) -> None:
    # Force the async writer to drain its queue without tearing the store
    # down — shutdown() only stops the writer thread; read connections are
    # opened fresh per call and remain usable afterward.
    store.shutdown()


class TestReadPacketsAsObjects:
    def test_round_trips_scalar_fields(self, store):
        store.save_packet(_pkt())
        _flush(store)
        [pkt] = store.read_packets_as_objects("sess-1")
        assert pkt.sender_num == 111111
        assert pkt.packet_id == 42
        assert pkt.portnum == 1
        assert pkt.portnum_name == "TEXT_MESSAGE_APP"
        assert pkt.rx_snr == 8.25
        assert pkt.rx_rssi == -72
        assert pkt.hops_used == 0
        assert pkt.via_mqtt is False
        assert pkt.pki_encrypted is False
        assert pkt.want_ack is True

    def test_text_is_always_none(self, store):
        # The packets table has no text column at all.
        store.save_packet(_pkt(text="this should not survive"))
        _flush(store)
        [pkt] = store.read_packets_as_objects("sess-1")
        assert pkt.text is None

    def test_unknown_via_mqtt_round_trips_as_none_not_false(self, store):
        store.save_packet(_pkt(via_mqtt=None))
        _flush(store)
        [pkt] = store.read_packets_as_objects("sess-1")
        assert pkt.via_mqtt is None

    def test_scoped_to_the_requested_session(self, store):
        store.save_packet(_pkt(session_id="sess-1", packet_id=1))
        store.save_packet(_pkt(session_id="sess-2", packet_id=2))
        _flush(store)
        pkts = store.read_packets_as_objects("sess-1")
        assert {p.packet_id for p in pkts} == {1}
