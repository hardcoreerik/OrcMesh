"""Tests for MonitorStore.update_message_status().

save_message() persisted only the initial SENDING row for an outbound
message; nothing ever updated it afterward, so restarting the app and
reloading history via read_messages() showed every past outbound message
stuck on "Sending…" forever, even ones the radio had actually accepted or
acknowledged.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

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


def _outbound_message() -> ChatMessage:
    return ChatMessage(
        local_id=str(uuid.uuid4()),
        packet_id=None,
        channel_index=0,
        direction=MessageDirection.OUTBOUND,
        sender_num=None,
        sender_id=None,
        sender_name="Me",
        text="hi",
        timestamp=datetime.now(timezone.utc),
        status=MessageStatus.SENDING,
    )


class TestUpdateMessageStatus:
    def test_status_update_persists_across_a_simulated_restart(self, store):
        msg = _outbound_message()
        store.save_message(msg)
        store.update_message_status(msg.local_id, 4660, MessageStatus.ACCEPTED_BY_RADIO)
        store.shutdown()

        # Simulate an app restart: a fresh MonitorStore over the same file,
        # reading back exactly what _load_message_history() would.
        reopened = MonitorStore(db_path=store._path)
        try:
            rows = reopened.read_messages()
            row = next(r for r in rows if r["local_id"] == msg.local_id)
            assert row["status"] == MessageStatus.ACCEPTED_BY_RADIO.value
            assert row["packet_id"] == 4660
        finally:
            reopened.shutdown()

    def test_status_update_with_no_packet_id_preserves_existing_one(self, store):
        msg = _outbound_message()
        store.save_message(msg)
        store.update_message_status(msg.local_id, 4660, MessageStatus.ACCEPTED_BY_RADIO)
        # A later event with no packet_id (e.g. a hypothetical ACK that
        # only carries status) must not clobber the one already recorded.
        store.update_message_status(msg.local_id, None, MessageStatus.ACKNOWLEDGED)
        store.shutdown()

        reopened = MonitorStore(db_path=store._path)
        try:
            row = next(r for r in reopened.read_messages() if r["local_id"] == msg.local_id)
            assert row["status"] == MessageStatus.ACKNOWLEDGED.value
            assert row["packet_id"] == 4660
        finally:
            reopened.shutdown()

    def test_status_update_for_unknown_local_id_does_not_raise(self, store):
        store.update_message_status("no-such-message", 1, MessageStatus.FAILED)
        store.shutdown()  # should not raise
