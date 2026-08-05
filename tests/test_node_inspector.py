"""Tests for NodeInspector's "hops left" display.

last_hop_start - last_hops_used was shown with no bounds check. Nothing
upstream guarantees hops_used <= hop_start for a malformed/out-of-order
packet, and an unclamped negative value would be confusing rather than
informative.
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv[:1])

from meshchat.models.node_snapshot import NodeSnapshot  # noqa: E402
from meshchat.ui.monitor.node_inspector import NodeInspector  # noqa: E402


def _snapshot(**overrides) -> NodeSnapshot:
    defaults = dict(node_num=1)
    defaults.update(overrides)
    return NodeSnapshot(**defaults)


class TestHopsLeft:
    def test_normal_case(self):
        inspector = NodeInspector()
        inspector.show_node(_snapshot(last_hop_start=5, last_hops_used=2))
        assert inspector._hops_left.text() == "3"

    def test_hops_used_exceeding_hop_start_is_clamped_to_zero(self):
        inspector = NodeInspector()
        inspector.show_node(_snapshot(last_hop_start=2, last_hops_used=5))
        assert inspector._hops_left.text() == "0"

    def test_missing_fields_show_placeholder(self):
        inspector = NodeInspector()
        inspector.show_node(_snapshot(last_hop_start=None, last_hops_used=None))
        assert inspector._hops_left.text() == "—"
