"""Fault-injection tests for MonitorStore's write queue durability.

Previously, a single bad item in a flush batch rolled back the *entire*
SQLite transaction (silently dropping every other item in that batch), and
there was no retry for transient errors or any visibility into dropped/
failed writes. This module locks in the fix: per-item isolation via
SAVEPOINTs, bounded retry with backoff for transient SQLite errors, and a
DatabaseWriterHealth snapshot that surfaces drops/failures/writer state.
"""
from __future__ import annotations

import queue
import sqlite3
import time
import unittest.mock as mock
from datetime import datetime, timezone

import pytest

from meshchat.controllers.meshtastic_controller import (
    ChatMessage,
    MessageDirection,
    MessageStatus,
)
from meshchat.models.network_session import NetworkSession
from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.models.position_sample import PositionSample
from meshchat.models.telemetry_sample import TelemetrySample
from meshchat.services.monitor_store import MonitorStore


@pytest.fixture
def store(tmp_path):
    s = MonitorStore(db_path=tmp_path / "test.db")
    yield s
    if s._writer.is_alive():
        s.shutdown()


def _message(text: str, local_id: str) -> ChatMessage:
    return ChatMessage(
        local_id=local_id,
        packet_id=None,
        channel_index=0,
        direction=MessageDirection.INBOUND,
        sender_num=1,
        sender_id="!00000001",
        sender_name="Sender",
        text=text,
        timestamp=datetime.now(timezone.utc),
        status=MessageStatus.RECEIVED,
    )


class TestPerItemIsolation:
    def test_one_permanently_failing_item_does_not_drop_the_rest_of_the_batch(self, store):
        with mock.patch.object(store, "_write_node", side_effect=ValueError("boom")):
            store.save_message(_message("before", "m1"))
            store.upsert_node(NodeSnapshot(node_num=1))
            store.save_message(_message("after", "m2"))
            store.shutdown()

        reopened = MonitorStore(db_path=store._path)
        try:
            texts = {r["text"] for r in reopened.read_messages()}
            assert texts == {"before", "after"}
        finally:
            reopened.shutdown()

    def test_failed_item_is_reflected_in_health(self, store):
        with mock.patch.object(store, "_write_node", side_effect=ValueError("boom")):
            store.upsert_node(NodeSnapshot(node_num=1))
            store.save_message(_message("survivor", "m1"))
            store.shutdown()

        assert store.health().failed_count == 1


class TestTransientRetry:
    def test_transient_error_is_retried_and_can_still_succeed(self, store):
        real_write_message = store._write_message
        calls = {"n": 0}

        def flaky(conn, msg):
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return real_write_message(conn, msg)

        with mock.patch.object(store, "_write_message", side_effect=flaky):
            store.save_message(_message("eventually saved", "m1"))
            store.shutdown()

        assert calls["n"] == 3
        assert store.health().failed_count == 0
        reopened = MonitorStore(db_path=store._path)
        try:
            texts = {r["text"] for r in reopened.read_messages()}
            assert "eventually saved" in texts
        finally:
            reopened.shutdown()

    def test_transient_error_exhausting_retries_fails_only_that_item(self, store):
        with mock.patch.object(
            store, "_write_position",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            store.save_position(PositionSample(
                node_num=1, observed_at=datetime.now(timezone.utc),
                latitude=1.0, longitude=2.0,
            ))
            store.save_session(NetworkSession(
                transport="tcp", connection_target="1.2.3.4",
                id="sess-1", started_at=datetime.now(timezone.utc),
            ))
            store.shutdown()

        health = store.health()
        assert health.failed_count == 1
        assert "locked" in (health.last_error or "").lower()

        conn = sqlite3.connect(str(store._path))
        try:
            row = conn.execute("SELECT COUNT(*) FROM sessions WHERE id='sess-1'").fetchone()
            assert row[0] == 1  # the unrelated item in the batch still landed
        finally:
            conn.close()

    def test_non_transient_error_is_not_retried(self, store):
        calls = {"n": 0}

        def always_fails(conn, tel):
            calls["n"] += 1
            raise ValueError("not a transient error")

        with mock.patch.object(store, "_write_telemetry", side_effect=always_fails):
            store.save_telemetry(TelemetrySample(
                node_num=1, observed_at=datetime.now(timezone.utc), battery_level=80,
            ))
            store.shutdown()

        assert calls["n"] == 1
        assert store.health().failed_count == 1


class TestQueueFullVisibility:
    def test_dropped_items_are_counted(self, store):
        with mock.patch.object(store._write_q, "put_nowait", side_effect=queue.Full):
            store.save_message(_message("dropped-1", "d1"))
            store.save_message(_message("dropped-2", "d2"))

        assert store.health().dropped_count == 2


class TestWriterCrashVisibility:
    def test_writer_thread_open_failure_marks_writer_dead_with_last_error(self, tmp_path, monkeypatch):
        real_connect = sqlite3.connect
        calls = {"n": 0}

        def flaky_connect(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:  # 1st call: __init__'s synchronous schema apply; 2nd: writer thread
                raise sqlite3.OperationalError("simulated unrecoverable open failure")
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", flaky_connect)
        s = MonitorStore(db_path=tmp_path / "test.db")
        try:
            for _ in range(50):
                if not s.health().writer_alive:
                    break
                time.sleep(0.05)
            health = s.health()
            assert health.writer_alive is False
            assert health.last_error is not None
        finally:
            if s._writer.is_alive():
                s.shutdown()
