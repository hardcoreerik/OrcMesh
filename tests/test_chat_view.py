"""Tests for ChatView.update_message_status()'s fallback matching.

Outbound bubbles are created with packet_id=None — the radio only assigns
a real packet_id once it accepts the send, a moment after the bubble
already exists on screen. The original exact-match-only implementation
(bubble._msg.packet_id == packet_id) could therefore never succeed for a
message's first status update, leaving it stuck showing "Sending…"
forever even though the radio accepted it.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv[:1])

from meshchat.controllers.meshtastic_controller import (  # noqa: E402
    ChatMessage,
    MessageDirection,
    MessageStatus,
)
from meshchat.ui.chat_view import ChatView  # noqa: E402


def _outbound(text: str = "hi") -> ChatMessage:
    return ChatMessage(
        local_id=str(uuid.uuid4()),
        packet_id=None,
        channel_index=0,
        direction=MessageDirection.OUTBOUND,
        sender_num=None,
        sender_id=None,
        sender_name="Me",
        text=text,
        timestamp=datetime.now(timezone.utc),
        status=MessageStatus.SENDING,
    )


class TestUpdateMessageStatusFallback:
    def test_first_status_update_matches_by_fallback_and_records_packet_id(self):
        view = ChatView()
        view.set_channel(0)
        msg = _outbound()
        view.add_message(msg)

        view.update_message_status(4660, MessageStatus.ACCEPTED_BY_RADIO)

        bubble = view._bubbles[msg.local_id]
        assert bubble._msg.packet_id == 4660
        assert bubble._msg.status == MessageStatus.ACCEPTED_BY_RADIO

    def test_second_status_update_matches_exactly_by_now_known_packet_id(self):
        view = ChatView()
        view.set_channel(0)
        msg = _outbound()
        view.add_message(msg)

        view.update_message_status(4660, MessageStatus.ACCEPTED_BY_RADIO)
        view.update_message_status(4660, MessageStatus.ACKNOWLEDGED)

        bubble = view._bubbles[msg.local_id]
        assert bubble._msg.status == MessageStatus.ACKNOWLEDGED

    def test_stored_message_is_updated_too_not_just_the_live_bubble(self):
        # _rebuild_bubbles() (e.g. on a channel switch) reconstructs
        # bubbles from _messages_by_key, not from the live bubble dict —
        # if only the bubble's _msg were replaced, switching away and back
        # would show "Sending…" again despite the earlier status update.
        view = ChatView()
        view.set_channel(0)
        msg = _outbound()
        view.add_message(msg)

        view.update_message_status(4660, MessageStatus.ACCEPTED_BY_RADIO)

        stored = view._messages_by_key[("channel", 0)][0]
        assert stored.packet_id == 4660
        assert stored.status == MessageStatus.ACCEPTED_BY_RADIO

    def test_oldest_pending_bubble_matched_first(self):
        view = ChatView()
        view.set_channel(0)
        first = _outbound("first")
        second = _outbound("second")
        view.add_message(first)
        view.add_message(second)

        view.update_message_status(111, MessageStatus.ACCEPTED_BY_RADIO)

        assert view._bubbles[first.local_id]._msg.packet_id == 111
        assert view._bubbles[second.local_id]._msg.packet_id is None

    def test_no_pending_sending_bubble_does_not_raise(self):
        view = ChatView()
        view.set_channel(0)
        view.update_message_status(999, MessageStatus.ACCEPTED_BY_RADIO)  # should not raise
