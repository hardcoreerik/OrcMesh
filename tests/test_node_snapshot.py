"""Tests for NodeSnapshot.is_direct.

Must mirror analytics.hop_metrics.is_direct_rf / NetworkPacket.is_direct_rf's
via_mqtt check — a zero-hop packet relayed over MQTT is not a direct RF
contact, it just preserved the original hop fields across the bridge. The
node-level property previously omitted that check, so a node whose most
recent packet was MQTT-bridged but reported hops_used == 0 would show a
false "Direct" badge in the Nodes table.
"""
from __future__ import annotations

from meshchat.models.node_snapshot import NodeSnapshot


def _snap(**overrides) -> NodeSnapshot:
    defaults = dict(node_num=1, last_hops_used=0, last_hop_start=3, last_via_mqtt=False)
    defaults.update(overrides)
    return NodeSnapshot(**defaults)


class TestIsDirect:
    def test_direct_rf_zero_hop_is_direct(self):
        assert _snap().is_direct is True

    def test_multi_hop_is_not_direct(self):
        assert _snap(last_hops_used=1).is_direct is False

    def test_mqtt_bridged_zero_hop_is_not_direct(self):
        assert _snap(last_via_mqtt=True).is_direct is False

    def test_unknown_transport_zero_hop_is_still_direct(self):
        # last_via_mqtt is None (never observed a via_mqtt flag) — only an
        # explicit True should suppress "direct", matching is_direct_rf's
        # "via_mqtt is not True" convention.
        assert _snap(last_via_mqtt=None).is_direct is True

    def test_missing_hop_start_is_not_direct(self):
        assert _snap(last_hop_start=None).is_direct is False

    def test_zero_hop_start_is_not_direct(self):
        assert _snap(last_hop_start=0).is_direct is False
