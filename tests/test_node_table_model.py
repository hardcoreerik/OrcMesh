"""Tests for the Nodes-page table model's cell rendering."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.ui.nodes.node_table_model import (
    _CELL_RENDERERS,
    _COLUMNS,
    _age,
    _source,
)


def _node(**kwargs) -> NodeSnapshot:
    base = {"node_num": 0x1234ABCD}
    base.update(kwargs)
    return NodeSnapshot(**base)


class TestColumnIntegrity:
    def test_every_column_has_a_renderer(self):
        # These two lists are edited independently; drift between them would
        # silently blank or misalign a column in the UI.
        assert len(_CELL_RENDERERS) == len(_COLUMNS)

    def test_every_renderer_returns_a_string_for_an_empty_node(self):
        node = _node()
        for i, render in enumerate(_CELL_RENDERERS):
            value = render(node)
            assert isinstance(value, str), f"column {i} ({_COLUMNS[i]}) returned {type(value)}"


class TestRenderers:
    def test_name_falls_back_through_to_hex_id(self):
        assert _CELL_RENDERERS[0](_node()) == "!1234abcd"
        assert _CELL_RENDERERS[0](_node(node_id="!abc")) == "!abc"
        assert _CELL_RENDERERS[0](_node(long_name="Base", node_id="!abc")) == "Base"

    def test_missing_values_render_as_dash(self):
        node = _node()
        assert _CELL_RENDERERS[1](node) == "—"   # short name
        assert _CELL_RENDERERS[3](node) == "—"   # role
        assert _CELL_RENDERERS[7](node) == "—"   # hops
        assert _CELL_RENDERERS[8](node) == "—"   # snr
        assert _CELL_RENDERERS[9](node) == "—"   # rssi

    def test_direct_marker_only_when_direct(self):
        direct = _node(last_hops_used=0, last_hop_start=3)
        relayed = _node(last_hops_used=2, last_hop_start=3)
        assert _CELL_RENDERERS[6](direct) == "✓"
        assert _CELL_RENDERERS[6](relayed) == ""

    def test_snr_is_formatted_to_one_decimal(self):
        assert _CELL_RENDERERS[8](_node(last_snr=6.25)) == "6.2"

    def test_negative_snr_renders(self):
        assert _CELL_RENDERERS[8](_node(last_snr=-12.5)) == "-12.5"

    def test_counts_render_as_strings(self):
        node = _node(packet_count=42, text_count=7)
        assert _CELL_RENDERERS[10](node) == "42"
        assert _CELL_RENDERERS[11](node) == "7"


class TestSource:
    def test_mqtt_when_mqtt_dominates(self):
        assert _source(_node(via_mqtt_count=5, rf_count=1)) == "MQTT"

    def test_rf_when_rf_dominates(self):
        assert _source(_node(via_mqtt_count=0, rf_count=3)) == "RF"

    def test_unknown_when_nothing_seen(self):
        assert _source(_node()) == "?"

    def test_tie_prefers_rf(self):
        # Equal counts is not "MQTT-dominant", so it should read as RF
        assert _source(_node(via_mqtt_count=2, rf_count=2)) == "RF"


class TestAge:
    def test_none_is_dash(self):
        assert _age(None) == "—"

    def test_seconds(self):
        ts = datetime.now(timezone.utc) - timedelta(seconds=5)
        assert _age(ts).endswith("s")

    def test_minutes(self):
        ts = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert _age(ts) == "5m"

    def test_hours(self):
        ts = datetime.now(timezone.utc) - timedelta(hours=3)
        assert _age(ts) == "3h"

    def test_days(self):
        ts = datetime.now(timezone.utc) - timedelta(days=2)
        assert _age(ts) == "2d"
