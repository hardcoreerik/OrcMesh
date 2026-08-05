"""Tests for PacketBreakdownPanel.update_packets()'s classification.

Previously used a local, separately-maintained portnum->label dict instead
of analytics/packet_classifier.packet_category(): it never checked
pki_encrypted at all, and treated an unset portnum as "Other Known"
instead of "Unknown Port" — so the panel's own "Unknown/Encrypted" row was
never populated, and encrypted/undecoded packets were silently miscounted
under "Other Known".
"""
from __future__ import annotations

from dataclasses import dataclass

from meshchat.ui.monitor.distribution_panel import PacketBreakdownPanel


@dataclass
class _FakePacket:
    portnum: int | None
    pki_encrypted: bool | None = False


class TestPacketBreakdownClassification:
    def test_encrypted_packet_counts_as_unknown_encrypted(self):
        panel = PacketBreakdownPanel()
        panel.update_packets([_FakePacket(portnum=None, pki_encrypted=True)])
        assert panel._rows["Unknown/Encrypted"]._count_lbl.text() == "1"

    def test_unset_portnum_counts_as_unknown_encrypted_not_other_known(self):
        panel = PacketBreakdownPanel()
        panel.update_packets([_FakePacket(portnum=None, pki_encrypted=False)])
        assert panel._rows["Unknown/Encrypted"]._count_lbl.text() == "1"
        assert panel._rows["Other Known"]._count_lbl.text() == "0"

    def test_genuinely_unrecognized_portnum_counts_as_other_known(self):
        panel = PacketBreakdownPanel()
        panel.update_packets([_FakePacket(portnum=999, pki_encrypted=False)])
        assert panel._rows["Other Known"]._count_lbl.text() == "1"

    def test_known_portnum_counts_under_its_own_label(self):
        panel = PacketBreakdownPanel()
        panel.update_packets([_FakePacket(portnum=1)])  # Text
        assert panel._rows["Text"]._count_lbl.text() == "1"

    def test_node_info_label_matches_canonical_classifier(self):
        # Regression for the "NodeInfo" (no space) vs "Node Info" (space)
        # label mismatch that silently dropped Node Info packets into
        # "Other Known" even though a row for them existed.
        panel = PacketBreakdownPanel()
        panel.update_packets([_FakePacket(portnum=4)])  # Node Info
        assert panel._rows["Node Info"]._count_lbl.text() == "1"
        assert panel._rows["Other Known"]._count_lbl.text() == "0"
