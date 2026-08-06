"""Regression tests for node_num/to-field zero-truthiness bugs.

node_num == 0 and packet "to" == 0 are legitimate (if unusual) protocol
values. Code that branches on `if value` instead of `if value is not None`
silently mishandles them. This module covers the two remaining live
instances of that bug class in meshtastic_controller.py; most others were
already fixed in earlier PRs (see ARCHITECTURE.md / git history).
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.controllers.meshtastic_controller import (
    MeshtasticWorker,
    _device_summary,
)


class _FakeMyInfo:
    def __init__(self, node_num):
        self.my_node_num = node_num


class _FakeInterface:
    def __init__(self, node_num, nodes_by_num, metadata=None):
        self.myInfo = _FakeMyInfo(node_num)
        self.nodesByNum = nodes_by_num
        self.metadata = metadata


class TestDeviceSummaryZeroNodeNum:
    def test_node_num_zero_looks_up_nodedb_entry(self):
        iface = _FakeInterface(
            node_num=0,
            nodes_by_num={0: {"user": {"id": "!00000000", "longName": "Zero Node"}}},
        )
        summary = _device_summary(iface)
        assert summary.node_num == 0
        assert summary.long_name == "Zero Node"

    def test_node_num_zero_falls_back_to_formatted_id_when_no_nodedb_entry(self):
        iface = _FakeInterface(node_num=0, nodes_by_num={})
        summary = _device_summary(iface)
        assert summary.node_num == 0
        assert summary.node_id == "!00000000"

    def test_node_num_none_yields_no_node_id(self):
        iface = _FakeInterface(node_num=None, nodes_by_num={1: {"user": {}}})
        summary = _device_summary(iface)
        assert summary.node_num is None
        assert summary.node_id is None


class TestInboundToFieldZero:
    def test_to_zero_is_not_treated_as_broadcast_destination(self):
        """A packet with to == 0 (not None, not BROADCAST_NUM) must resolve
        destination_num from the real `to` value, not fall through to
        toNum — `to or toNum` would incorrectly prefer toNum for to == 0."""
        worker = MeshtasticWorker()
        received = []
        worker.message_received.connect(received.append)

        packet = {
            "from": 0x1111,
            "fromId": "!00001111",
            "to": 0,
            "toNum": 0xDEADBEEF,  # must NOT be used — "to" is present and 0
            "id": 42,
            "channel": 0,
            "decoded": {"text": "hi"},
        }
        # _is_active_interface requires interface is self._interface; set it
        # directly to bypass the connect flow for this unit test.
        worker._interface = object()
        worker._on_text_received(packet=packet, interface=worker._interface)

        assert len(received) == 1
        msg = received[0]
        # to=0 is not BROADCAST_NUM and not None, so this is a direct message.
        assert msg.destination_num == msg.sender_num
