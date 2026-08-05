"""Tests for utils.packet_parser."""
import json
from pathlib import Path


from meshchat.utils.packet_parser import ParsedTextMessage, parse_text_packet

# Load sanitised fixtures
_FIXTURES_PATH = Path(__file__).parent / "fixtures" / "packets.json"
_FIXTURES = json.loads(_FIXTURES_PATH.read_text(encoding="utf-8"))


# ── parse_text_packet ─────────────────────────────────────────────────────────

class TestParseTextPacket:
    def test_direct_text_basic_fields(self):
        pkt = _FIXTURES["direct_text"]
        msg = parse_text_packet(pkt)
        assert msg is not None
        assert isinstance(msg, ParsedTextMessage)
        assert msg.text == "Hello direct"
        assert msg.sender_num == 111111
        assert msg.sender_id == "!0001b1c7"
        assert msg.channel_index == 0
        assert msg.rx_snr == 12.5
        assert msg.rx_rssi == -68

    def test_direct_text_hop_fields(self):
        pkt = _FIXTURES["direct_text"]
        msg = parse_text_packet(pkt)
        assert msg.hop_start == 3
        assert msg.hop_limit == 3
        assert msg.hops_used == 0

    def test_one_hop_text(self):
        pkt = _FIXTURES["one_hop_text"]
        msg = parse_text_packet(pkt)
        assert msg is not None
        assert msg.text == "Hello one hop"
        assert msg.hops_used == 1

    def test_multi_hop_text(self):
        pkt = _FIXTURES["multi_hop_text"]
        msg = parse_text_packet(pkt)
        assert msg is not None
        assert msg.hops_used == 2
        assert msg.channel_index == 1

    def test_mqtt_text_no_snr(self):
        pkt = _FIXTURES["mqtt_text"]
        msg = parse_text_packet(pkt)
        assert msg is not None
        assert msg.text == "Hello via MQTT"
        assert msg.rx_snr is None
        assert msg.rx_rssi is None

    def test_hop_start_zero_hops_used_is_none(self):
        """hopStart == 0 → hops_used cannot be computed."""
        pkt = _FIXTURES["unknown_hop_start_zero"]
        msg = parse_text_packet(pkt)
        assert msg is not None
        assert msg.hops_used is None

    def test_sender_name_from_nodes_by_num(self):
        pkt = _FIXTURES["direct_text"]
        nodes = {
            111111: {
                "user": {
                    "longName": "Alice Node",
                    "shortName": "ALIC",
                    "id": "!0001b1c7",
                }
            }
        }
        msg = parse_text_packet(pkt, nodes_by_num=nodes)
        assert msg is not None
        assert msg.sender_long_name == "Alice Node"
        assert msg.sender_short_name == "ALIC"

    def test_sender_name_not_in_nodes_returns_none_names(self):
        pkt = _FIXTURES["direct_text"]
        msg = parse_text_packet(pkt, nodes_by_num={})
        assert msg.sender_long_name is None
        assert msg.sender_short_name is None

    def test_returns_none_for_non_text_portnum(self):
        """A NodeInfo packet has no text field; parse should return None."""
        pkt = _FIXTURES["node_info"]
        result = parse_text_packet(pkt)
        assert result is None

    def test_returns_none_for_malformed_no_decoded(self):
        pkt = _FIXTURES["malformed_no_decoded"]
        result = parse_text_packet(pkt)
        assert result is None

    def test_returns_none_for_decoded_not_dict(self):
        pkt = _FIXTURES["malformed_decoded_not_dict"]
        result = parse_text_packet(pkt)
        assert result is None

    def test_text_from_bytes_payload(self):
        """Text can also come from bytes in decoded.payload."""
        pkt = {
            "from": 12345,
            "id": 99001,
            "channel": 0,
            "decoded": {
                "portnum": 1,
                "payload": b"Hello bytes",
            },
        }
        msg = parse_text_packet(pkt)
        assert msg is not None
        assert msg.text == "Hello bytes"

    def test_text_from_string_payload_fallback(self):
        """decoded.payload can also be a plain string."""
        pkt = {
            "from": 12345,
            "id": 99002,
            "channel": 0,
            "decoded": {
                "portnum": 1,
                "payload": "Hello str payload",
            },
        }
        msg = parse_text_packet(pkt)
        assert msg is not None
        assert msg.text == "Hello str payload"

    def test_channel_index_defaults_to_zero(self):
        pkt = {
            "from": 12345,
            "id": 99003,
            "decoded": {"portnum": 1, "text": "hi"},
        }
        msg = parse_text_packet(pkt)
        assert msg is not None
        assert msg.channel_index == 0

    def test_camel_case_and_snake_case_hop_fields(self):
        """Both hopStart/hopLimit and hop_start/hop_limit should be accepted."""
        pkt = {
            "from": 12345,
            "id": 99004,
            "channel": 0,
            "hop_start": 3,
            "hop_limit": 2,
            "decoded": {"portnum": 1, "text": "snake case"},
        }
        msg = parse_text_packet(pkt)
        assert msg is not None
        assert msg.hops_used == 1
