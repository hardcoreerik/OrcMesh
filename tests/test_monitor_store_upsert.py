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
