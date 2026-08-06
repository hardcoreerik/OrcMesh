"""Regression test for MonitorStore.read_messages() truncation.

`ORDER BY observed_at ASC LIMIT ?` sorts oldest-first *before* applying the
limit, so once the table holds more rows than the limit, the query returned
the oldest N messages and silently dropped everything newer — the opposite
of what a bounded "load recent chat history" query should do.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from meshchat.controllers.meshtastic_controller import (
    ChatMessage,
    MessageDirection,
    MessageStatus,
)
from meshchat.services.monitor_store import MonitorStore


@pytest.fixture
def store(tmp_path):
    s = MonitorStore(db_path=tmp_path / "test.db")
    yield s
    s.shutdown()


def _message(index: int, base: datetime, *, destination_num=None) -> ChatMessage:
    return ChatMessage(
        local_id=str(uuid.uuid4()),
        packet_id=index,
        channel_index=0,
        direction=MessageDirection.INBOUND,
        sender_num=100,
        sender_id="!00000064",
        sender_name="Sender",
        text=f"message {index}",
        timestamp=base + timedelta(seconds=index),
        status=MessageStatus.RECEIVED,
        destination_num=destination_num,
    )


class TestReadMessagesTruncation:
    def test_returns_newest_messages_not_oldest_when_over_limit(self, store):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        total = 20
        limit = 5
        for i in range(total):
            store.save_message(_message(i, base))
        store.shutdown()

        reopened = MonitorStore(db_path=store._path)
        try:
            rows = reopened.read_messages(limit=limit)
            texts = [r["text"] for r in rows]
            # Must be the newest `limit` messages: "message 15".."message 19".
            assert texts == [f"message {i}" for i in range(total - limit, total)]
        finally:
            reopened.shutdown()

    def test_result_is_in_chronological_order(self, store):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for i in range(10):
            store.save_message(_message(i, base))
        store.shutdown()

        reopened = MonitorStore(db_path=store._path)
        try:
            rows = reopened.read_messages(limit=4)
            timestamps = [r["observed_at"] for r in rows]
            assert timestamps == sorted(timestamps)
        finally:
            reopened.shutdown()

    def test_direct_and_channel_messages_both_present_when_over_limit(self, store):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for i in range(10):
            store.save_message(_message(i, base))
        # A newer batch of direct messages, interleaved with more channel ones.
        for i in range(10, 16):
            dm = i % 2 == 0
            store.save_message(_message(i, base, destination_num=200 if dm else None))
        store.shutdown()

        reopened = MonitorStore(db_path=store._path)
        try:
            rows = reopened.read_messages(limit=6)
            assert len(rows) == 6
            dm_rows = [r for r in rows if r["destination_num"] is not None]
            channel_rows = [r for r in rows if r["destination_num"] is None]
            # Both groups survive the newest-N cut — neither is dropped wholesale.
            assert dm_rows
            assert channel_rows
        finally:
            reopened.shutdown()

    def test_under_limit_still_returns_everything_in_order(self, store):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for i in range(3):
            store.save_message(_message(i, base))
        store.shutdown()

        reopened = MonitorStore(db_path=store._path)
        try:
            rows = reopened.read_messages(limit=100)
            assert [r["text"] for r in rows] == ["message 0", "message 1", "message 2"]
        finally:
            reopened.shutdown()
