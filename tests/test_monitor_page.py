"""Tests for MonitorPage's FilterBar wiring.

FilterBar's Source/Type dropdowns were entirely decorative: current_filter()
returned {"age_s", "source", "portnum"}, but _refresh_rankings() only ever
read "age_s" (and only for the Active KPI card) — the Source ("RF only" /
"MQTT-path" / "Unknown transport") and Type (packet category) dropdowns had
zero effect on any ranking, distribution panel, or node count. Age itself
was applied to the Active card's node count but not to the packet/node data
every other panel is built from.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from meshchat.models.network_packet import NetworkPacket
from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.ui.monitor.monitor_page import MonitorPage, _matches_packet_type


def _pkt(sender_num=1, portnum=1, via_mqtt=False, pki_encrypted=False, age_s=0) -> NetworkPacket:
    return NetworkPacket(
        session_id="s",
        observed_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
        rx_time=None,
        sender_num=sender_num,
        sender_id=None,
        destination_num=None,
        packet_id=None,
        channel_index=0,
        portnum=portnum,
        portnum_name="",
        text=None,
        payload_size=None,
        rx_snr=None,
        rx_rssi=None,
        hop_start=3,
        hop_limit=3,
        hops_used=0,
        via_mqtt=via_mqtt,
        transport_mechanism=None,
        pki_encrypted=pki_encrypted,
        want_ack=None,
        priority=None,
        raw_metadata_json="{}",
    )


class TestMatchesPacketType:
    def test_text_matches_text_category(self):
        assert _matches_packet_type(_pkt(portnum=1), "Text") is True
        assert _matches_packet_type(_pkt(portnum=1), "Position") is False

    def test_unknown_port_and_encrypted_both_match_the_combined_option(self):
        assert _matches_packet_type(_pkt(portnum=None), "Unknown/Encrypted") is True
        assert _matches_packet_type(_pkt(portnum=1, pki_encrypted=True), "Unknown/Encrypted") is True


class TestMonitorPageFilterWiring:
    def test_source_filter_excludes_non_matching_packets_from_rankings(self):
        page = MonitorPage()
        page.on_packet_ingested(_pkt(sender_num=1, via_mqtt=False))
        page.on_packet_ingested(_pkt(sender_num=2, via_mqtt=True))

        page._active_filter = {"age_s": None, "source": "RF only", "portnum": "All"}
        page._refresh_rankings()
        senders = {row._node_num for row in page._rank_most_pkts._rows}
        assert senders == {1}, "RF only must exclude the MQTT-bridged packet's sender"

        page._active_filter = {"age_s": None, "source": "MQTT-path", "portnum": "All"}
        page._refresh_rankings()
        senders = {row._node_num for row in page._rank_most_pkts._rows}
        assert senders == {2}, "MQTT-path must exclude the RF packet's sender"

    def test_type_filter_excludes_non_matching_packets_from_rankings(self):
        page = MonitorPage()
        page.on_packet_ingested(_pkt(sender_num=1, portnum=1))   # Text
        page.on_packet_ingested(_pkt(sender_num=2, portnum=3))   # Position

        page._active_filter = {"age_s": None, "source": "All observed", "portnum": "Text"}
        page._refresh_rankings()
        senders = {row._node_num for row in page._rank_most_pkts._rows}
        assert senders == {1}

    def test_age_filter_excludes_old_packets_and_nodes(self):
        page = MonitorPage()
        page.on_packet_ingested(_pkt(sender_num=1, age_s=0))
        page.on_packet_ingested(_pkt(sender_num=2, age_s=7200))  # 2 hours old
        page.on_node_updated(NodeSnapshot(node_num=1, last_heard=datetime.now(timezone.utc)))
        page.on_node_updated(NodeSnapshot(
            node_num=2, last_heard=datetime.now(timezone.utc) - timedelta(seconds=7200),
        ))

        page._active_filter = {"age_s": 300, "source": "All observed", "portnum": "All"}
        page._refresh_rankings()

        senders = {row._node_num for row in page._rank_most_pkts._rows}
        assert senders == {1}, "5-minute age window must exclude the 2-hour-old packet"
        heard = {row._node_num for row in page._rank_last_heard._rows}
        assert heard == {1}, "5-minute age window must exclude the node last heard 2 hours ago"
        assert page._card_total._value_lbl.text() == "1"

    def test_all_age_keeps_everything(self):
        page = MonitorPage()
        page.on_packet_ingested(_pkt(sender_num=1, age_s=0))
        page.on_packet_ingested(_pkt(sender_num=2, age_s=7200))

        page._active_filter = {"age_s": None, "source": "All observed", "portnum": "All"}
        page._refresh_rankings()
        senders = {row._node_num for row in page._rank_most_pkts._rows}
        assert senders == {1, 2}
