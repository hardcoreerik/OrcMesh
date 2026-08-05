"""Tests for outgoing-message validation.

The broadcast and direct send paths were ~90% identical copies. They now share
_prepare_outgoing()/_dispatch_text(), so these tests pin the rules once and
assert both paths actually go through them.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.controllers.meshtastic_controller import (  # noqa: E402
    MAX_MESSAGE_BYTES,
    ConnectionState,
    ErrorCode,
    MessageStatus,
    MeshtasticWorker,
)


class _FakeInterface:
    def __init__(self):
        self.sent: list[dict] = []

    def sendText(self, **kwargs):  # noqa: N802 - mirrors the meshtastic API
        self.sent.append(kwargs)
        return type("Pkt", (), {"id": 4242})()


def _connected_worker():
    w = MeshtasticWorker()
    w._state = ConnectionState.CONNECTED
    w._interface = _FakeInterface()
    return w


def _errors_from(worker) -> list:
    captured = []
    worker.error_occurred.connect(captured.append)
    return captured


class TestByteLimit:
    def test_limit_constant_is_sane(self):
        assert 0 < MAX_MESSAGE_BYTES <= 240

    def test_message_at_the_limit_is_sent(self):
        w = _connected_worker()
        w.send_channel_text("a" * MAX_MESSAGE_BYTES, 0, "local-1")
        assert len(w._interface.sent) == 1

    def test_message_over_the_limit_is_rejected(self):
        w = _connected_worker()
        errors = _errors_from(w)
        w.send_channel_text("a" * (MAX_MESSAGE_BYTES + 1), 0, "local-1")
        assert w._interface.sent == []
        assert errors and errors[0].code == ErrorCode.INVALID_MESSAGE

    def test_limit_is_bytes_not_characters(self):
        # Emoji are 4 UTF-8 bytes each; 60 of them exceed a 200-byte limit
        # while being only 60 characters.
        w = _connected_worker()
        errors = _errors_from(w)
        w.send_channel_text("😀" * 60, 0, "local-1")
        assert w._interface.sent == []
        assert errors

    def test_error_message_quotes_the_shared_constant(self):
        w = _connected_worker()
        errors = _errors_from(w)
        w.send_channel_text("a" * (MAX_MESSAGE_BYTES + 5), 0, "local-1")
        assert str(MAX_MESSAGE_BYTES) in errors[0].message


class TestNormalisation:
    def test_crlf_is_normalised(self):
        w = _connected_worker()
        w.send_channel_text("line1\r\nline2", 0, "local-1")
        assert w._interface.sent[0]["text"] == "line1\nline2"

    def test_lone_cr_is_normalised(self):
        w = _connected_worker()
        w.send_channel_text("line1\rline2", 0, "local-1")
        assert w._interface.sent[0]["text"] == "line1\nline2"

    def test_surrounding_whitespace_is_stripped(self):
        w = _connected_worker()
        w.send_channel_text("  hello  ", 0, "local-1")
        assert w._interface.sent[0]["text"] == "hello"

    def test_whitespace_only_is_a_silent_no_op(self):
        w = _connected_worker()
        errors = _errors_from(w)
        w.send_channel_text("   \n  ", 0, "local-1")
        assert w._interface.sent == []
        assert errors == [], "empty input should not raise a user-facing error"


class TestNotConnected:
    def test_channel_send_while_disconnected_errors(self):
        w = MeshtasticWorker()
        w._state = ConnectionState.DISCONNECTED
        errors = _errors_from(w)
        w.send_channel_text("hi", 0, "local-1")
        assert errors and errors[0].code == ErrorCode.SEND_FAILED

    def test_direct_send_while_disconnected_errors(self):
        w = MeshtasticWorker()
        w._state = ConnectionState.DISCONNECTED
        errors = _errors_from(w)
        w.send_direct_text("hi", 123, "local-1")
        assert errors and errors[0].code == ErrorCode.SEND_FAILED


class TestRouting:
    def test_channel_send_broadcasts_on_the_given_channel(self):
        w = _connected_worker()
        w.send_channel_text("hi", 3, "local-1")
        sent = w._interface.sent[0]
        assert sent["destinationId"] == "^all"
        assert sent["channelIndex"] == 3

    def test_direct_send_targets_the_node(self):
        w = _connected_worker()
        w.send_direct_text("hi", 99887766, "local-1")
        sent = w._interface.sent[0]
        assert sent["destinationId"] == 99887766
        # A direct message must not be broadcast on a channel
        assert "channelIndex" not in sent

    def test_both_paths_request_ack(self):
        w = _connected_worker()
        w.send_channel_text("a", 0, "local-1")
        w.send_direct_text("b", 1, "local-1")
        assert all(s["wantAck"] for s in w._interface.sent)

    def test_both_paths_enforce_the_same_limit(self):
        # The point of the shared helper: rules cannot diverge between paths.
        too_long = "a" * (MAX_MESSAGE_BYTES + 1)
        for send in (
            lambda w: w.send_channel_text(too_long, 0, "local-1"),
            lambda w: w.send_direct_text(too_long, 5, "local-1"),
        ):
            w = _connected_worker()
            send(w)
            assert w._interface.sent == []


class TestSendFailure:
    def test_radio_exception_surfaces_as_a_recoverable_error(self):
        w = _connected_worker()

        def boom(**kwargs):
            raise RuntimeError("radio unplugged")

        w._interface.sendText = boom
        errors = _errors_from(w)
        w.send_channel_text("hi", 0, "local-1")
        assert errors and errors[0].code == ErrorCode.SEND_FAILED
        assert errors[0].recoverable is True

    def test_successful_send_reports_accepted_status(self):
        w = _connected_worker()
        statuses = []
        w.message_status_changed.connect(lambda lid, pid, st, detail: statuses.append((lid, pid, st)))
        w.send_channel_text("hi", 0, "local-1")
        assert statuses and statuses[0][0] == "local-1" and statuses[0][1] == 4242

    def test_failed_send_reports_failed_status_with_the_local_id(self):
        w = _connected_worker()

        def boom(**kwargs):
            raise RuntimeError("radio unplugged")

        w._interface.sendText = boom
        statuses = []
        w.message_status_changed.connect(lambda lid, pid, st, detail: statuses.append((lid, pid, st)))
        w.send_channel_text("hi", 0, "local-1")
        assert statuses == [("local-1", None, MessageStatus.FAILED)]

    def test_send_while_disconnected_still_reports_failed_status(self):
        # A bubble already exists client-side by the time this runs —
        # without a status event here, it would be orphaned on "Sending…"
        # forever.
        w = MeshtasticWorker()
        w._state = ConnectionState.DISCONNECTED
        statuses = []
        w.message_status_changed.connect(lambda lid, pid, st, detail: statuses.append((lid, pid, st)))
        w.send_channel_text("hi", 0, "local-1")
        assert statuses == [("local-1", None, MessageStatus.FAILED)]


class TestNormalizationIsShared:
    """The composer's byte counter and the send path must measure the same
    string, or Send greys out on messages that are actually valid."""

    def test_trailing_crlf_does_not_inflate_the_count(self):
        from meshchat.controllers.meshtastic_controller import normalize_outgoing_text

        raw = "a" * (MAX_MESSAGE_BYTES - 1) + "\r\n"
        assert len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES, "precondition"
        # After normalisation it is within the limit, so the composer must
        # count it as sendable rather than blocking it.
        assert len(normalize_outgoing_text(raw).encode("utf-8")) <= MAX_MESSAGE_BYTES

    def test_that_message_actually_sends(self):
        w = _connected_worker()
        w.send_channel_text("a" * (MAX_MESSAGE_BYTES - 1) + "\r\n", 0, "local-1")
        assert len(w._interface.sent) == 1
        assert w._interface.sent[0]["text"] == "a" * (MAX_MESSAGE_BYTES - 1)

    def test_normalizer_is_idempotent(self):
        from meshchat.controllers.meshtastic_controller import normalize_outgoing_text

        once = normalize_outgoing_text("  a\r\nb\r  ")
        assert normalize_outgoing_text(once) == once

    def test_composer_and_controller_agree_on_length(self):
        # Property-ish check across shapes that previously disagreed.
        from meshchat.controllers.meshtastic_controller import normalize_outgoing_text

        for raw in ("hi\r\n", "  hi  ", "a\rb", "\r\n\r\n", "x" * 50 + "\r\n"):
            composer_len = len(normalize_outgoing_text(raw).encode("utf-8"))
            w = _connected_worker()
            w.send_channel_text(raw, 0, "local-1")
            sent = w._interface.sent[0]["text"] if w._interface.sent else ""
            assert composer_len == len(sent.encode("utf-8")), f"disagreed on {raw!r}"
