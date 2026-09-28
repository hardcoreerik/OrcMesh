"""Tests for per-node packet intelligence.

Synthetic packets with known payloads and arrival times, so every airtime figure
and trend can be checked against arithmetic done here rather than against the
implementation.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from meshchat.analytics.lora_airtime import airtime_seconds, params_for_preset
from meshchat.analytics.packet_intel import (
    MIN_TREND_SAMPLES,
    TREND_FALLING,
    TREND_RISING,
    TREND_STEADY,
    TREND_UNKNOWN,
    analyse_packets,
)
from meshchat.models.network_packet import NetworkPacket

_BASE = datetime(2026, 9, 27, 12, 0, 0)
_FAST = params_for_preset("LONG_FAST")   # SF11 / 250 kHz
_SLOW = params_for_preset("LONG_SLOW")   # SF12 / 125 kHz

_ALPHA = 0x11111111
_BRAVO = 0x22222222


def _packet(
    *,
    sender: int | None = _ALPHA,
    minutes: float = 0.0,
    payload: int | None = 16,
    snr: float | None = 5.0,
    rssi: int | None = -60,
    hops: int | None = 0,
    hop_start: int | None = 3,
    via_mqtt: bool = False,
    portnum: str = "TEXT_MESSAGE_APP",
) -> NetworkPacket:
    """A packet with everything boring filled in.

    Defaults describe a directly-heard RF packet, which is the case most of these
    tests vary away from.
    """
    hop_limit = hop_start - hops if (hop_start is not None and hops is not None) else None
    return NetworkPacket(
        session_id="session",
        observed_at=_BASE + timedelta(minutes=minutes),
        rx_time=None,
        sender_num=sender,
        sender_id=None,
        destination_num=None,
        packet_id=None,
        channel_index=0,
        portnum=1,
        portnum_name=portnum,
        text=None,
        payload_size=payload,
        rx_snr=snr,
        rx_rssi=rssi,
        hop_start=hop_start,
        hop_limit=hop_limit,
        hops_used=hops,
        via_mqtt=via_mqtt,
        transport_mechanism=None,
        pki_encrypted=None,
        want_ack=None,
        priority=None,
        raw_metadata_json=None,
    )


class TestAirtime:
    def test_it_uses_the_modem_parameters_it_was_given(self):
        packets = [_packet()]

        fast = analyse_packets(packets, _FAST)
        slow = analyse_packets(packets, _SLOW)

        assert slow.total_airtime_s > fast.total_airtime_s * 3

    def test_the_total_is_the_sum_of_the_nodes(self):
        packets = [_packet(sender=_ALPHA), _packet(sender=_BRAVO), _packet(sender=_ALPHA)]

        report = analyse_packets(packets, _FAST)

        assert report.total_airtime_s == pytest.approx(
            sum(node.airtime_s for node in report.nodes)
        )

    def test_a_node_sending_more_holds_more_of_the_channel(self):
        packets = [_packet(sender=_BRAVO) for _ in range(4)]
        packets.append(_packet(sender=_ALPHA))

        report = analyse_packets(packets, _FAST)

        assert report.busiest is not None
        assert report.busiest.node_num == _BRAVO
        assert report.busiest.airtime_s == pytest.approx(4 * airtime_seconds(16, _FAST))

    def test_shares_add_up(self):
        packets = [_packet(sender=_ALPHA) for _ in range(3)]
        packets += [_packet(sender=_BRAVO) for _ in range(1)]

        report = analyse_packets(packets, _FAST)

        assert sum(node.airtime_share for node in report.nodes) == pytest.approx(1.0)

    def test_a_bigger_payload_costs_more(self):
        small = analyse_packets([_packet(payload=8)], _FAST)
        large = analyse_packets([_packet(payload=160)], _FAST)

        assert large.total_airtime_s > small.total_airtime_s

    def test_the_duty_cycle_compares_airtime_to_the_window(self):
        packets = [_packet(minutes=0), _packet(minutes=1)]

        report = analyse_packets(packets, _FAST)

        assert report.window_s == pytest.approx(60.0)
        assert report.channel_duty_cycle == pytest.approx(report.total_airtime_s / 60.0)


class TestPartialData:
    def test_packets_with_no_size_contribute_no_airtime_but_are_counted(self):
        """Counting them silently as zero would hide an undercount."""
        packets = [_packet(payload=16), _packet(payload=None), _packet(payload=None)]

        report = analyse_packets(packets, _FAST)

        assert report.analysed_packets == 3
        assert report.unsized_packets == 2
        assert report.total_airtime_s == pytest.approx(airtime_seconds(16, _FAST))
        assert report.overhead == pytest.approx(2 / 3)

    def test_overhead_is_zero_when_every_size_is_known(self):
        report = analyse_packets([_packet(), _packet()], _FAST)

        assert report.overhead == 0.0

    def test_packets_with_no_sender_are_reported_separately(self):
        packets = [_packet(sender=None), _packet(sender=_ALPHA)]

        report = analyse_packets(packets, _FAST)

        assert report.unattributed_packets == 1
        assert report.analysed_packets == 1
        assert len(report.nodes) == 1

    def test_a_node_with_no_signal_readings_still_appears(self):
        report = analyse_packets([_packet(snr=None, rssi=None)], _FAST)

        assert report.nodes[0].signal.sample_count == 0
        assert report.nodes[0].signal.median_snr is None


class TestRouting:
    def test_direct_and_relayed_and_mqtt_are_told_apart(self):
        packets = [
            _packet(hops=0, hop_start=3),                 # direct
            _packet(hops=1, hop_start=3),                 # relayed
            _packet(hops=2, hop_start=3),                 # relayed
            _packet(hops=3, hop_start=3, via_mqtt=True),  # over the internet
        ]

        node = analyse_packets(packets, _FAST).nodes[0]

        assert node.direct_packets == 1
        assert node.relayed_packets == 2
        assert node.via_mqtt_packets == 1

    def test_a_zero_hop_packet_is_not_direct_when_hop_start_is_unknown(self):
        """Older firmware reports hop_start 0, which cannot prove directness."""
        packets = [_packet(hops=0, hop_start=0)]

        node = analyse_packets(packets, _FAST).nodes[0]

        assert node.direct_packets == 0
        assert node.relayed_packets == 1

    def test_a_zero_hop_packet_over_mqtt_is_not_direct(self):
        packets = [_packet(hops=0, hop_start=3, via_mqtt=True)]

        node = analyse_packets(packets, _FAST).nodes[0]

        assert node.direct_packets == 0
        assert node.via_mqtt_packets == 1

    def test_direct_nodes_are_the_ones_heard_nearby(self):
        packets = [_packet(sender=_ALPHA, hops=0), _packet(sender=_BRAVO, hops=2)]

        report = analyse_packets(packets, _FAST)

        assert [node.node_num for node in report.direct] == [_ALPHA]


class TestForeignNodes:
    def test_a_node_the_mesh_has_no_history_with_is_flagged(self):
        """This is the SIGINT signal: a radio that knows the key and is new."""
        packets = [_packet(sender=_ALPHA)]
        packets += [_packet(sender=_BRAVO, hops=0, snr=-2.0) for _ in range(2)]

        report = analyse_packets(packets, _FAST, known_nodes={_ALPHA})

        assert [node.node_num for node in report.foreign] == [_BRAVO]
        assert report.nodes[0].is_foreign, "a newcomer can be the busiest node"
        alpha = next(node for node in report.nodes if node.node_num == _ALPHA)
        assert not alpha.is_foreign

    def test_everything_is_foreign_when_nothing_is_known(self):
        packets = [_packet(sender=_ALPHA), _packet(sender=_BRAVO)]

        report = analyse_packets(packets, _FAST)

        assert len(report.foreign) == 2

    def test_nothing_is_foreign_when_everything_is_known(self):
        packets = [_packet(sender=_ALPHA), _packet(sender=_BRAVO)]

        report = analyse_packets(packets, _FAST, known_nodes={_ALPHA, _BRAVO})

        assert report.foreign == ()


class TestSignalTrend:
    def test_a_rising_signal_is_spotted(self):
        packets = [_packet(minutes=index, snr=value) for index, value in enumerate([-4, -2, 0, 2, 6, 9])]

        assert analyse_packets(packets, _FAST).nodes[0].snr_trend == TREND_RISING

    def test_a_falling_signal_is_spotted(self):
        packets = [_packet(minutes=index, snr=value) for index, value in enumerate([9, 7, 5, 1, -2, -5])]

        assert analyse_packets(packets, _FAST).nodes[0].snr_trend == TREND_FALLING

    def test_a_steady_signal_is_not_called_a_trend(self):
        packets = [_packet(minutes=index, snr=value) for index, value in enumerate([5, 5.5, 4.8, 5.2, 5.1, 4.9])]

        assert analyse_packets(packets, _FAST).nodes[0].snr_trend == TREND_STEADY

    def test_too_few_packets_gives_no_verdict(self):
        """Two readings is not a trend; a short session says so rather than guessing."""
        packets = [_packet(minutes=index, snr=value) for index, value in enumerate([-5, 9])]

        assert analyse_packets(packets, _FAST).nodes[0].snr_trend == TREND_UNKNOWN

    def test_the_threshold_is_where_it_says_it_is(self):
        assert MIN_TREND_SAMPLES == 4

        packets = [_packet(minutes=index, snr=value) for index, value in enumerate([0, 0, 0, 1.0])]

        assert analyse_packets(packets, _FAST).nodes[0].snr_trend == TREND_STEADY

    def test_the_trend_follows_arrival_time_not_ingest_order(self):
        """Packets arrive in whatever order the radio delivered them.

        A rising signal fed in reverse must still read as rising. Taking the
        readings in list order instead would invert the answer here.
        """
        rising = [_packet(minutes=index, snr=value) for index, value in enumerate([-4, -1, 2, 5, 8, 11])]

        shuffled = [rising[3], rising[0], rising[5], rising[1], rising[4], rising[2]]

        assert analyse_packets(shuffled, _FAST).nodes[0].snr_trend == TREND_RISING
        assert analyse_packets(list(reversed(rising)), _FAST).nodes[0].snr_trend == TREND_RISING

    def test_each_node_gets_its_own_trend(self):
        packets = [_packet(sender=_ALPHA, minutes=i, snr=v) for i, v in enumerate([-4, -1, 2, 5])]
        packets += [_packet(sender=_BRAVO, minutes=i, snr=v) for i, v in enumerate([5, 2, -1, -4])]

        report = analyse_packets(packets, _FAST)

        by_node = {node.node_num: node for node in report.nodes}
        assert by_node[_ALPHA].snr_trend == TREND_RISING
        assert by_node[_BRAVO].snr_trend == TREND_FALLING


class TestRates:
    def test_the_window_is_first_to_last_packet(self):
        packets = [_packet(minutes=0), _packet(minutes=2), _packet(minutes=10)]

        assert analyse_packets(packets, _FAST).window_s == pytest.approx(600.0)

    def test_packets_per_minute(self):
        packets = [_packet(minutes=0), _packet(minutes=1), _packet(minutes=2)]

        node = analyse_packets(packets, _FAST).nodes[0]

        assert node.packets_per_minute == pytest.approx(1.5)

    def test_a_single_packet_has_no_rate_rather_than_a_division_error(self):
        node = analyse_packets([_packet()], _FAST).nodes[0]

        assert node.packets_per_minute == 0.0

    def test_first_and_last_seen(self):
        packets = [_packet(minutes=5), _packet(minutes=1)]

        node = analyse_packets(packets, _FAST).nodes[0]

        assert node.first_seen == _BASE + timedelta(minutes=1)
        assert node.last_heard == _BASE + timedelta(minutes=5)


class TestLabelsAndPorts:
    def test_a_known_name_is_used(self):
        report = analyse_packets([_packet(sender=_ALPHA)], _FAST, labels={_ALPHA: "Tower"})

        assert report.nodes[0].label == "Tower"

    def test_an_unknown_node_falls_back_to_its_hex_id(self):
        report = analyse_packets([_packet(sender=_ALPHA)], _FAST)

        assert report.nodes[0].label == f"!{_ALPHA:08x}"

    def test_an_empty_name_does_not_win_over_the_hex_id(self):
        report = analyse_packets([_packet(sender=_ALPHA)], _FAST, labels={_ALPHA: ""})

        assert report.nodes[0].label == f"!{_ALPHA:08x}"

    def test_portnums_are_counted_most_common_first(self):
        packets = [
            _packet(portnum="POSITION_APP"),
            _packet(portnum="TEXT_MESSAGE_APP"),
            _packet(portnum="TEXT_MESSAGE_APP"),
        ]

        report = analyse_packets(packets, _FAST)

        assert report.nodes[0].portnums[0] == ("TEXT_MESSAGE_APP", 2)
        assert report.nodes[0].top_portnum == "TEXT_MESSAGE_APP"
        assert report.portnum_totals[0] == ("TEXT_MESSAGE_APP", 2)

    def test_a_node_with_no_packets_has_no_top_portnum(self):
        report = analyse_packets([], _FAST)

        assert report.nodes == ()
        assert report.busiest is None
        assert report.channel_duty_cycle == 0.0


class TestRanking:
    def test_nodes_are_ordered_by_airtime(self):
        packets = [_packet(sender=_BRAVO, payload=16)]
        packets += [_packet(sender=_ALPHA, payload=16) for _ in range(3)]

        report = analyse_packets(packets, _FAST)

        assert [node.node_num for node in report.nodes] == [_ALPHA, _BRAVO]

    def test_equal_airtime_orders_deterministically(self):
        """Equal traffic must not reshuffle the table between refreshes."""
        packets = [_packet(sender=_BRAVO), _packet(sender=_ALPHA)]

        first = analyse_packets(packets, _FAST)
        second = analyse_packets(list(reversed(packets)), _FAST)

        assert [node.node_num for node in first.nodes] == [_ALPHA, _BRAVO]
        assert [node.node_num for node in first.nodes] == [
            node.node_num for node in second.nodes
        ]
