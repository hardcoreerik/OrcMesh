"""Tests for MonitorStore retention pruning.

Packets, positions, and telemetry were written forever with nothing ever
removing them. Node identities and chat messages must stay exempt: the node
database is the app's memory of the mesh, and messages are user content.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from meshchat.services.monitor_store import DEFAULT_RETAIN_DAYS, MonitorStore


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def store(tmp_path):
    s = MonitorStore(db_path=tmp_path / "test.db")
    yield s
    s.shutdown()


def _seed(store: MonitorStore, ages_days: list[float]) -> None:
    conn = sqlite3.connect(str(store._path))
    with conn:
        for i, age in enumerate(ages_days):
            ts = _iso(age)
            conn.execute(
                "INSERT INTO packets (session_id, observed_at, sender_num) VALUES (?,?,?)",
                ("s", ts, 100 + i),
            )
            conn.execute(
                "INSERT INTO positions (node_num, observed_at, latitude, longitude) "
                "VALUES (?,?,?,?)",
                (100 + i, ts, 44.0, -123.0),
            )
            conn.execute(
                "INSERT INTO telemetry (node_num, observed_at, voltage) VALUES (?,?,?)",
                (100 + i, ts, 4.1),
            )
        conn.execute(
            "INSERT INTO nodes (node_num, node_id, last_heard) VALUES (?,?,?)",
            (999, "!old", _iso(365)),
        )
        conn.execute(
            "INSERT INTO messages (session_id, local_id, observed_at, text) VALUES (?,?,?,?)",
            ("s", "m-old", _iso(365), "ancient but precious"),
        )
    conn.close()


def _count(store: MonitorStore, table: str) -> int:
    conn = sqlite3.connect(str(store._path))
    n = conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    conn.close()
    return n


class TestPrune:
    def test_deletes_rows_older_than_retention(self, store):
        _seed(store, [1, 5, 60, 90])
        store.prune(retain_days=30)
        assert _count(store, "packets") == 2  # the 1- and 5-day rows survive

    def test_keeps_recent_rows(self, store):
        _seed(store, [0.5, 2])
        store.prune(retain_days=30)
        assert _count(store, "packets") == 2

    def test_prunes_all_three_volume_tables(self, store):
        _seed(store, [90])
        store.prune(retain_days=30)
        for table in ("packets", "positions", "telemetry"):
            assert _count(store, table) == 0, f"{table} was not pruned"

    def test_never_prunes_nodes(self, store):
        # The node DB is the app's memory of the mesh — a year-old node
        # identity is still how we render a name for that node.
        _seed(store, [90])
        store.prune(retain_days=30)
        assert _count(store, "nodes") == 1

    def test_never_prunes_messages(self, store):
        # Chat history is user content; silently deleting it is not acceptable.
        _seed(store, [90])
        store.prune(retain_days=30)
        assert _count(store, "messages") == 1

    def test_returns_counts_per_table(self, store):
        _seed(store, [90, 100])
        deleted = store.prune(retain_days=30)
        assert deleted["packets"] == 2
        assert deleted["positions"] == 2
        assert deleted["telemetry"] == 2

    def test_returns_empty_when_nothing_to_delete(self, store):
        _seed(store, [1])
        assert store.prune(retain_days=30) == {}

    @pytest.mark.parametrize("retain", [0, -1])
    def test_non_positive_retention_keeps_everything(self, store, retain):
        _seed(store, [365])
        assert store.prune(retain_days=retain) == {}
        assert _count(store, "packets") == 1

    def test_default_retention_is_sane(self):
        assert 1 <= DEFAULT_RETAIN_DAYS <= 365

    def test_prune_on_empty_database_is_harmless(self, store):
        assert store.prune(retain_days=30) == {}
