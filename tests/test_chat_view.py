"""Tests for ChatView.update_message_status().

Originally matched only by packet_id, which is unknown at bubble-creation
time (the radio only assigns it after accepting the send) and never
assigned at all on a failed send — so the match could never succeed for a
message's first status update, leaving it stuck showing "Sending…"
forever. Fixed by matching on local_id instead, which is generated
client-side and threaded through the whole send path, so it's always
known and unambiguous — including for a send that fails outright, and
for a message on a channel/DM other than the one currently shown.
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


def _outbound(text: str = "hi", destination_num: int | None = None) -> ChatMessage:
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
        destination_num=destination_num,
    )


class TestUpdateMessageStatus:
    def test_first_status_update_matches_by_local_id_and_records_packet_id(self):
        view = ChatView()
        view.set_channel(0)
        msg = _outbound()
        view.add_message(msg)

        view.update_message_status(msg.local_id, 4660, MessageStatus.ACCEPTED_BY_RADIO)

        bubble = view._bubbles[msg.local_id]
        assert bubble._msg.packet_id == 4660
        assert bubble._msg.status == MessageStatus.ACCEPTED_BY_RADIO

    def test_second_status_update_keeps_matching_by_local_id(self):
        view = ChatView()
        view.set_channel(0)
        msg = _outbound()
        view.add_message(msg)

        view.update_message_status(msg.local_id, 4660, MessageStatus.ACCEPTED_BY_RADIO)
        view.update_message_status(msg.local_id, 4660, MessageStatus.ACKNOWLEDGED)

        assert view._bubbles[msg.local_id]._msg.status == MessageStatus.ACKNOWLEDGED

    def test_stored_message_is_updated_too_not_just_the_live_bubble(self):
        # _rebuild_bubbles() (e.g. on a channel switch) reconstructs
        # bubbles from _messages_by_key, not from the live bubble dict —
        # if only the bubble's _msg were replaced, switching away and back
        # would show "Sending…" again despite the earlier status update.
        view = ChatView()
        view.set_channel(0)
        msg = _outbound()
        view.add_message(msg)

        view.update_message_status(msg.local_id, 4660, MessageStatus.ACCEPTED_BY_RADIO)

        stored = view._messages_by_key[("channel", 0)][0]
        assert stored.packet_id == 4660
        assert stored.status == MessageStatus.ACCEPTED_BY_RADIO

    def test_multiple_pending_sends_each_matched_correctly(self):
        # The old FIFO-fallback design broke here: it always matched the
        # OLDEST pending "Sending…" bubble, regardless of which one the
        # status event was actually for.
        view = ChatView()
        view.set_channel(0)
        first = _outbound("first")
        second = _outbound("second")
        view.add_message(first)
        view.add_message(second)

        view.update_message_status(second.local_id, 222, MessageStatus.ACCEPTED_BY_RADIO)

        assert view._bubbles[first.local_id]._msg.packet_id is None
        assert view._bubbles[first.local_id]._msg.status == MessageStatus.SENDING
        assert view._bubbles[second.local_id]._msg.packet_id == 222
        assert view._bubbles[second.local_id]._msg.status == MessageStatus.ACCEPTED_BY_RADIO

    def test_status_update_for_a_non_visible_thread_still_updates_storage(self):
        # The old design only ever searched the currently-visible
        # _bubbles, so a status update for a message on a different
        # channel/DM than the one shown was silently dropped.
        view = ChatView()
        view.set_channel(0)
        msg = _outbound()
        view.add_message(msg)

        view.set_dm_target(999, "Someone Else")  # switch away — msg's bubble no longer exists
        assert msg.local_id not in view._bubbles

        view.update_message_status(msg.local_id, 4660, MessageStatus.ACCEPTED_BY_RADIO)

        stored = view._messages_by_key[("channel", 0)][0]
        assert stored.packet_id == 4660
        assert stored.status == MessageStatus.ACCEPTED_BY_RADIO

    def test_failed_send_with_no_packet_id_still_matches_and_applies_status(self):
        view = ChatView()
        view.set_channel(0)
        msg = _outbound()
        view.add_message(msg)

        view.update_message_status(msg.local_id, None, MessageStatus.FAILED)

        bubble = view._bubbles[msg.local_id]
        assert bubble._msg.status == MessageStatus.FAILED
        assert bubble._msg.packet_id is None

    def test_unknown_local_id_does_not_raise(self):
        view = ChatView()
        view.set_channel(0)
        view.update_message_status("no-such-id", 999, MessageStatus.ACCEPTED_BY_RADIO)  # should not raise
