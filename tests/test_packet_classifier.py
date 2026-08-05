"""Tests for analytics.packet_classifier."""
import pytest
from meshchat.analytics.packet_classifier import (
    classify_portnum,
    is_nodeinfo_packet,
    is_position_packet,
    is_telemetry_packet,
    is_text_packet,
    packet_category,
)


# ── classify_portnum ──────────────────────────────────────────────────────────

class TestClassifyPortnum:
    def test_text_message(self):
        assert classify_portnum(1) == "Text"

    def test_position(self):
        assert classify_portnum(3) == "Position"

    def test_node_info(self):
        assert classify_portnum(4) == "Node Info"

    def test_routing(self):
        assert classify_portnum(5) == "Routing"

    def test_telemetry(self):
        assert classify_portnum(67) == "Telemetry"

    def test_traceroute(self):
        assert classify_portnum(70) == "Traceroute"

    def test_neighbor_info(self):
        assert classify_portnum(71) == "Neighbor Info"

    def test_map_report(self):
        assert classify_portnum(73) == "Map Report"

    def test_private_app(self):
        assert classify_portnum(256) == "Private App"

    def test_unknown_portnum_zero(self):
        assert classify_portnum(0) == "Unknown Port"

    def test_none_is_unknown(self):
        assert classify_portnum(None) == "Unknown Port"

    def test_unlisted_portnum(self):
        # Any portnum not in the known table → "Other Known (N)"
        result = classify_portnum(9999)
        assert "9999" in result


# ── packet_category ───────────────────────────────────────────────────────────

class TestPacketCategory:
    def test_text(self):
        assert packet_category(1) == "Text"

    def test_position(self):
        assert packet_category(3) == "Position"

    def test_node_info(self):
        assert packet_category(4) == "Node Info"

    def test_routing(self):
        assert packet_category(5) == "Routing"

    def test_telemetry(self):
        assert packet_category(67) == "Telemetry"

    def test_traceroute(self):
        assert packet_category(70) == "Traceroute"

    def test_neighbor_info(self):
        assert packet_category(71) == "Neighbor Info"

    def test_map_report(self):
        assert packet_category(73) == "Map Report"

    def test_private_app(self):
        assert packet_category(256) == "Private App"

    def test_unknown_port_zero(self):
        assert packet_category(0) == "Unknown Port"

    def test_none_portnum(self):
        assert packet_category(None) == "Unknown Port"

    def test_pki_encrypted_overrides_portnum(self):
        """PKI-encrypted packets should be categorised as Encrypted/Undecoded
        regardless of what portnum they carry."""
        assert packet_category(1, pki_encrypted=True) == "Encrypted/Undecoded"
        assert packet_category(None, pki_encrypted=True) == "Encrypted/Undecoded"

    def test_pki_none_not_encrypted(self):
        assert packet_category(1, pki_encrypted=None) == "Text"

    def test_pki_false_not_encrypted(self):
        assert packet_category(1, pki_encrypted=False) == "Text"

    def test_unlisted_portnum_other_known(self):
        assert packet_category(9999) == "Other Known"


# ── boolean helpers ───────────────────────────────────────────────────────────

class TestBooleanHelpers:
    def test_is_text_packet(self):
        assert is_text_packet(1) is True
        assert is_text_packet(3) is False
        assert is_text_packet(None) is False

    def test_is_position_packet(self):
        assert is_position_packet(3) is True
        assert is_position_packet(1) is False

    def test_is_telemetry_packet(self):
        assert is_telemetry_packet(67) is True
        assert is_telemetry_packet(1) is False

    def test_is_nodeinfo_packet(self):
        assert is_nodeinfo_packet(4) is True
        assert is_nodeinfo_packet(3) is False
