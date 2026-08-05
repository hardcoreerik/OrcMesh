"""Tests for NetworkSession.start_new()/.end().

A reconnect in the same app run previously kept stamping messages/packets
with the FIRST connection's session_id forever, since MainWindow only ever
constructed one NetworkSession (in __init__) and nothing reset it on
reconnect. start_new() must mutate the existing object in place — not
return a new one — since PacketIngestor holds and mutates this same
instance directly.
"""
from __future__ import annotations

from meshchat.models.network_session import NetworkSession


class TestStartNew:
    def test_mutates_in_place_same_identity(self):
        session = NetworkSession.new()
        same_object = session
        session.start_new()
        assert session is same_object

    def test_generates_a_new_id(self):
        session = NetworkSession.new()
        first_id = session.id
        session.start_new()
        assert session.id != first_id

    def test_resets_packet_count(self):
        session = NetworkSession.new()
        session.packet_count = 42
        session.start_new()
        assert session.packet_count == 0

    def test_clears_ended_at(self):
        session = NetworkSession.new()
        session.end()
        assert session.ended_at is not None
        session.start_new()
        assert session.ended_at is None

    def test_shared_reference_sees_the_new_id(self):
        # Simulates PacketIngestor holding the same NetworkSession instance
        # MainWindow does — the whole point of mutating in place.
        session = NetworkSession.new()

        class _FakeIngestor:
            def __init__(self, s):
                self._session = s

        ingestor = _FakeIngestor(session)
        session.start_new()
        assert ingestor._session.id == session.id


class TestEnd:
    def test_sets_ended_at(self):
        session = NetworkSession.new()
        assert session.ended_at is None
        session.end()
        assert session.ended_at is not None
