"""Tests for utils.node_names."""
from meshchat.utils.node_names import resolve_node_name, resolve_short_name


_NODES = {
    111111: {
        "user": {
            "id": "!0001b1c7",
            "longName": "Alice Node",
            "shortName": "ALIC",
        }
    },
    222222: {
        "user": {
            "id": "!000364ce",
            "shortName": "BOB",
            # No longName — tests short name fallback
        }
    },
    333333: {
        "user": {
            "id": "!00051615",
            # Neither longName nor shortName
        }
    },
}


# ── resolve_node_name ─────────────────────────────────────────────────────────

class TestResolveNodeName:
    def test_long_name_takes_priority(self):
        name = resolve_node_name(111111, nodes_by_num=_NODES)
        assert name == "Alice Node"

    def test_short_name_fallback_when_no_long_name(self):
        name = resolve_node_name(222222, nodes_by_num=_NODES)
        assert name == "BOB"

    def test_node_id_fallback_in_db_but_no_names(self):
        """When NodeDB has only an id, fall through to node_id arg."""
        name = resolve_node_name(333333, node_id="!00051615", nodes_by_num=_NODES)
        # NodeDB user has id but no longName/shortName → fall through to node_id param
        assert name == "!00051615"

    def test_node_id_arg_fallback_when_not_in_db(self):
        name = resolve_node_name(999999, node_id="!000f423f", nodes_by_num=_NODES)
        assert name == "!000f423f"

    def test_formatted_num_fallback(self):
        name = resolve_node_name(12345678, nodes_by_num=_NODES)
        assert name == "!00bc614e"

    def test_unknown_node_when_no_info(self):
        name = resolve_node_name(None, node_id=None, nodes_by_num=None)
        assert name == "Unknown node"

    def test_empty_nodes_by_num_uses_node_id(self):
        name = resolve_node_name(111111, node_id="!0001b1c7", nodes_by_num={})
        assert name == "!0001b1c7"

    def test_none_nodes_by_num_uses_node_id(self):
        name = resolve_node_name(111111, node_id="!0001b1c7", nodes_by_num=None)
        assert name == "!0001b1c7"

    def test_snake_case_long_name(self):
        """Accepts long_name (snake_case) as well as longName."""
        nodes = {
            500: {"user": {"long_name": "Snake Node", "short_name": "SNK"}}
        }
        assert resolve_node_name(500, nodes_by_num=nodes) == "Snake Node"

    def test_node_num_zero_is_looked_up_not_treated_as_falsy(self):
        # node_num == 0 is a legitimate (if unusual) node number — `and
        # node_num` would treat it as falsy and skip the NodeDB lookup
        # entirely, falling through to the formatted-number fallback
        # instead of the real name.
        nodes = {0: {"user": {"longName": "Zero Node"}}}
        assert resolve_node_name(0, nodes_by_num=nodes) == "Zero Node"


# ── resolve_short_name ────────────────────────────────────────────────────────

class TestResolveShortName:
    def test_short_name_from_db(self):
        assert resolve_short_name(111111, nodes_by_num=_NODES) == "ALIC"

    def test_short_name_from_db_no_long_name(self):
        assert resolve_short_name(222222, nodes_by_num=_NODES) == "BOB"

    def test_last_4_of_node_id(self):
        # node 333333 has no shortName in DB; node_id="!00051615" → last 4 = "1615"
        result = resolve_short_name(333333, node_id="!00051615", nodes_by_num=_NODES)
        assert result == "1615"

    def test_hex_num_fallback(self):
        result = resolve_short_name(12345678, nodes_by_num={})
        # 12345678 decimal = 0xbc614e; format is {:04x} (min-width 4, not truncated)
        assert result == "bc614e"

    def test_unknown_fallback(self):
        assert resolve_short_name(None) == "????"

    def test_short_node_id_not_truncated(self):
        """A node_id shorter than 4 chars should not be truncated."""
        result = resolve_short_name(99999, node_id="!ab", nodes_by_num={})
        assert result == "!ab"

    def test_node_num_zero_is_looked_up_not_treated_as_falsy(self):
        nodes = {0: {"user": {"shortName": "ZERO"}}}
        assert resolve_short_name(0, nodes_by_num=nodes) == "ZERO"
