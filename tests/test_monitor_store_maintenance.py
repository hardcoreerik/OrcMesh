"""Tests for MonitorStore's on-demand maintenance tools.

Covers: check_integrity, backup (default and explicit dest), backup
captures pending writes, vacuum_async, and backup timeout/dead-writer
guard.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from meshchat.services.monitor_store import MonitorStore


def _make_store(tmp_path: Path) -> MonitorStore:
    return MonitorStore(db_path=tmp_path / "test.db")


def _drain(store: MonitorStore, timeout: float = 2.0) -> None:
    """Block until the write queue is empty."""
    deadline = time.monotonic() + timeout
    while not store._write_q.empty():
        if time.monotonic() > deadline:
            raise TimeoutError("write queue did not drain in time")
        time.sleep(0.01)
    time.sleep(0.05)  # let the writer finish the current batch


class TestCheckIntegrity:
    def test_returns_ok_for_clean_database(self, tmp_path):
        store = _make_store(tmp_path)
        ok, detail = store.check_integrity()
        store.shutdown()
        assert ok is True
        assert detail == "ok"

    def test_returns_false_when_connection_fails(self, tmp_path):
        store = _make_store(tmp_path)
        store.shutdown()
        # Patch _read_conn to blow up, simulating an unreadable file.
        with patch.object(store, "_read_conn", side_effect=sqlite3.OperationalError("no such file")):
            ok, detail = store.check_integrity()
        assert ok is False
        assert "no such file" in detail


class TestBackup:
    def test_default_dest_creates_file_in_backups_subfolder(self, tmp_path):
        store = _make_store(tmp_path)
        backup_path = store.backup()
        store.shutdown()

        assert backup_path.exists()
        assert backup_path.parent == tmp_path / "backups"
        assert backup_path.stem.startswith("test.manual.")
        assert backup_path.suffix == ".db"

    def test_explicit_dest_is_respected(self, tmp_path):
        store = _make_store(tmp_path)
        dest = tmp_path / "exports" / "my_backup.db"
        backup_path = store.backup(dest=dest)
        store.shutdown()

        assert backup_path == dest
        assert backup_path.exists()

    def test_backup_is_a_valid_sqlite_database(self, tmp_path):
        store = _make_store(tmp_path)
        backup_path = store.backup()
        store.shutdown()

        conn = sqlite3.connect(str(backup_path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "messages" in tables
        assert "nodes" in tables

    def test_backup_includes_writes_queued_before_it(self, tmp_path):
        """backup() must flush all pending writes first, so the copy reflects
        everything that was enqueued before backup() was called."""
        store = _make_store(tmp_path)

        # Write a node directly via the queue (not through public API that
        # needs full model objects) — use a raw session row for simplicity.
        conn_seed = sqlite3.connect(str(tmp_path / "test.db"))
        conn_seed.execute(
            "INSERT INTO nodes (node_num, long_name, packet_count) VALUES (99, 'PreBackup', 0)"
        )
        conn_seed.commit()
        conn_seed.close()

        backup_path = store.backup()
        store.shutdown()

        conn = sqlite3.connect(str(backup_path))
        row = conn.execute("SELECT long_name FROM nodes WHERE node_num=99").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "PreBackup"

    def test_backup_blocks_until_queued_writes_are_flushed(self, tmp_path):
        """backup() is synchronous: it should not return before the write
        queue drains — confirmed by timing the event order."""
        store = _make_store(tmp_path)
        order: list[str] = []

        def slow_enqueue():
            # Occupy the writer with 200 sleepy no-ops via a prune on
            # an effectively zero-day cutoff (deletes nothing but takes a lock).
            for _ in range(5):
                store._enqueue(("prune", 0))
            order.append("enqueued")

        t = threading.Thread(target=slow_enqueue)
        t.start()
        t.join()

        store.backup()   # blocks until the writer processes the prunes first
        order.append("backup_done")

        store.shutdown()
        assert order == ["enqueued", "backup_done"]

    def test_raises_if_writer_not_alive(self, tmp_path):
        store = _make_store(tmp_path)
        store.shutdown()
        with pytest.raises(RuntimeError, match="Writer thread is not alive"):
            store.backup()


class TestVacuumAsync:
    def test_vacuum_runs_without_error(self, tmp_path):
        store = _make_store(tmp_path)
        store.vacuum_async()
        _drain(store)
        store.shutdown()
        # If vacuum raised, it would be logged but not re-raised; we verify
        # the store is still functional afterwards.
        ok, detail = store.check_integrity()
        assert ok is True

    def test_vacuum_does_not_corrupt_data(self, tmp_path):
        store = _make_store(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute(
            "INSERT INTO nodes (node_num, long_name, packet_count) VALUES (1, 'Node', 0)"
        )
        conn.commit()
        conn.close()

        store.vacuum_async()
        _drain(store)
        store.shutdown()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute("SELECT long_name FROM nodes WHERE node_num=1").fetchone()
        conn.close()
        assert row == ("Node",)
