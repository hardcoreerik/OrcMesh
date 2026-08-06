"""Tests for MonitorStore.upsert_node's ON CONFLICT merge behavior.

upsert_node() used to unconditionally overwrite last_heard/packet_count/
text_count with whatever the caller passed, even if that was older/smaller
than what was already persisted. The in-memory NodeSnapshot these values
come from is monotonic within one running app instance, but the DB write
itself shouldn't silently depend on that — this locks in that the merge is
safe even when called with stale-looking data.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.services.monitor_store import MonitorStore


@pytest.fixture
def store(tmp_path):
    s = MonitorStore(db_path=tmp_path / "test.db")
    yield s
    s.shutdown()


def _row(store: MonitorStore, node_num: int) -> sqlite3.Row:
    conn = sqlite3.connect(str(store._path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM nodes WHERE node_num=?", (node_num,)).fetchone()
    conn.close()
    return row


class TestUpsertNodeMerge:
    def test_older_upsert_does_not_regress_packet_count(self, store):
        now = datetime.now(timezone.utc)
        store.upsert_node(NodeSnapshot(node_num=1, last_heard=now, packet_count=50, text_count=5))
        store.upsert_node(NodeSnapshot(
            node_num=1, last_heard=now - timedelta(hours=1), packet_count=3, text_count=0,
        ))
        store.shutdown()
        row = _row(store, 1)
        assert row["packet_count"] == 50
        assert row["text_count"] == 5

    def test_newer_upsert_still_advances_counts(self, store):
        now = datetime.now(timezone.utc)
        store.upsert_node(NodeSnapshot(node_num=2, last_heard=now, packet_count=10, text_count=1))
        store.upsert_node(NodeSnapshot(
            node_num=2, last_heard=now + timedelta(minutes=5), packet_count=20, text_count=2,
        ))
        store.shutdown()
        row = _row(store, 2)
        assert row["packet_count"] == 20
        assert row["text_count"] == 2

    def test_first_upsert_with_null_last_heard_then_real_one(self, store):
        # Seeded with no last_heard yet (e.g. a node known only from the
        # radio's NodeDB, never actually heard this session).
        store.upsert_node(NodeSnapshot(node_num=3, last_heard=None, packet_count=0, text_count=0))
        now = datetime.now(timezone.utc)
        store.upsert_node(NodeSnapshot(node_num=3, last_heard=now, packet_count=1, text_count=0))
        store.shutdown()
        row = _row(store, 3)
        assert row["last_heard"] is not None

    def test_second_upsert_with_null_last_heard_keeps_existing_timestamp(self, tmp_path):
        s = MonitorStore(db_path=tmp_path / "test2.db")
        now = datetime.now(timezone.utc)
        s.upsert_node(NodeSnapshot(node_num=4, last_heard=now, packet_count=1, text_count=0))
        s.upsert_node(NodeSnapshot(node_num=4, last_heard=None, packet_count=2, text_count=0))
        s.shutdown()
        row = _row(s, 4)
        assert row["last_heard"] is not None
        assert row["packet_count"] == 2


class TestUpsertNodeSignalHopTransportPersistence:
    """last_snr/last_rssi/last_hops_used/last_hop_start/last_via_mqtt and the
    rf_count/via_mqtt_count/position_count/telemetry_count counters used to
    have no DB column at all — a node the app had tracked for weeks reset to
    zero/unknown on every restart until it was heard again live."""

    def test_signal_and_hop_fields_round_trip(self, store):
        now = datetime.now(timezone.utc)
        store.upsert_node(NodeSnapshot(
            node_num=5, last_heard=now,
            last_snr=8.25, last_rssi=-72, last_hops_used=0, last_hop_start=3,
            last_via_mqtt=False, rf_count=4, via_mqtt_count=1,
            position_count=2, telemetry_count=1,
        ))
        store.shutdown()
        row = _row(store, 5)
        assert row["last_snr"] == 8.25
        assert row["last_rssi"] == -72
        assert row["last_hops_used"] == 0
        assert row["last_hop_start"] == 3
        assert row["last_via_mqtt"] == 0
        assert row["rf_count"] == 4
        assert row["via_mqtt_count"] == 1
        assert row["position_count"] == 2
        assert row["telemetry_count"] == 1

    def test_older_upsert_does_not_overwrite_newer_last_hops_used(self, store):
        # A fresher 0-hop packet must not lose to a staler 3-hop one just
        # because a naive MAX() would prefer the bigger number.
        now = datetime.now(timezone.utc)
        store.upsert_node(NodeSnapshot(
            node_num=6, last_heard=now, last_hops_used=0, last_hop_start=3,
        ))
        store.upsert_node(NodeSnapshot(
            node_num=6, last_heard=now - timedelta(hours=1),
            last_hops_used=3, last_hop_start=3,
        ))
        store.shutdown()
        row = _row(store, 6)
        assert row["last_hops_used"] == 0

    def test_newer_upsert_advances_last_snr(self, store):
        now = datetime.now(timezone.utc)
        store.upsert_node(NodeSnapshot(node_num=7, last_heard=now, last_snr=4.0))
        store.upsert_node(NodeSnapshot(
            node_num=7, last_heard=now + timedelta(minutes=5), last_snr=9.5,
        ))
        store.shutdown()
        row = _row(store, 7)
        assert row["last_snr"] == 9.5

    def test_counters_use_max_not_last_observation(self, store):
        store.upsert_node(NodeSnapshot(node_num=8, rf_count=5, via_mqtt_count=2))
        store.upsert_node(NodeSnapshot(node_num=8, rf_count=3, via_mqtt_count=1))
        store.shutdown()
        row = _row(store, 8)
        assert row["rf_count"] == 5
        assert row["via_mqtt_count"] == 2
