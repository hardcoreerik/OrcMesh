"""Per-node signal intelligence, derived from what the radio has actually heard.

The Monitor page already shows *current* state — node tables, the latest signal
readings, a hop distribution. This module answers the questions that need the
traffic as a whole: which node is consuming the most channel time, whose signal is
strengthening or fading, who is being heard directly rather than relayed, and
which radios have appeared that this mesh has no history with.

Airtime is the part nothing else provides. A radio reports its own
``air_util_tx``, but no packet carries how long someone *else* held the channel;
that comes from the payload size and the modem parameters, via
``analytics.lora_airtime``.

Pure: packets in, profiles out. No Qt, no store.
"""
from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from meshchat.analytics.lora_airtime import ModemParams, airtime_seconds, duty_cycle
from meshchat.analytics.signal_metrics import SignalStats, compute_signal_stats
from meshchat.models.network_packet import NetworkPacket

#: A signal change smaller than this is noise between two halves of a session, not
#: a trend. RSSI/SNR wander by a few dB with multipath and body position alone.
TREND_THRESHOLD_DB = 2.0

#: Fewer samples than this and a "trend" is two lucky readings. Within a session
#: a node often has only a handful of packets.
MIN_TREND_SAMPLES = 4

TREND_RISING = "rising"
TREND_FALLING = "falling"
TREND_STEADY = "steady"
TREND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class NodeIntel:
    """What the analysed traffic says about one node."""

    node_num: int
    label: str
    packets: int
    airtime_s: float
    airtime_share: float
    packets_per_minute: float
    direct_packets: int
    relayed_packets: int
    via_mqtt_packets: int
    signal: SignalStats
    snr_trend: str
    first_seen: datetime | None
    last_heard: datetime | None
    portnums: tuple[tuple[str, int], ...]
    is_unfamiliar: bool

    @property
    def is_foreign(self) -> bool:
        """Heard over RF while this mesh has no prior record of the node.

        A radio only decodes packets whose channel key it shares, so this is not
        "an unknown protocol" — it is a node that knows this mesh's key and that
        OrcMesh has never seen before, which is what makes it worth surfacing.
        """
        return self.is_unfamiliar

    @property
    def talkativity(self) -> float:
        """Packets per minute — a node transmitting far more than its neighbours."""
        return self.packets_per_minute

    @property
    def top_portnum(self) -> str:
        return self.portnums[0][0] if self.portnums else "—"


@dataclass(frozen=True)
class IntelReport:
    """The whole picture for one window of traffic."""

    nodes: tuple[NodeIntel, ...]
    window_s: float
    total_airtime_s: float
    channel_duty_cycle: float
    analysed_packets: int
    unsized_packets: int
    unattributed_packets: int
    params_label: str
    portnum_totals: tuple[tuple[str, int], ...] = ()

    @property
    def busiest(self) -> NodeIntel | None:
        """The node consuming the most channel time."""
        return max(self.nodes, key=lambda node: node.airtime_s, default=None)

    @property
    def foreign(self) -> tuple[NodeIntel, ...]:
        return tuple(node for node in self.nodes if node.is_foreign)

    @property
    def direct(self) -> tuple[NodeIntel, ...]:
        """Nodes heard with a confirmed zero-hop RF packet — nearby radios."""
        return tuple(node for node in self.nodes if node.direct_packets > 0)

    @property
    def overhead(self) -> float:
        """Fraction of payload sizes that were missing, 0..1.

        Reported rather than hidden: airtime for those packets is counted as
        zero, so a high value means the totals below are an undercount.
        """
        if not self.analysed_packets:
            return 0.0
        return self.unsized_packets / self.analysed_packets


def _split_trend(values: Sequence[float]) -> str:
    """Classify the change between the first and second half of a series."""
    if len(values) < MIN_TREND_SAMPLES:
        return TREND_UNKNOWN
    midpoint = len(values) // 2
    first, second = values[:midpoint], values[midpoint:]
    if not first or not second:
        return TREND_UNKNOWN
    change = statistics.median(second) - statistics.median(first)
    if change > TREND_THRESHOLD_DB:
        return TREND_RISING
    if change < -TREND_THRESHOLD_DB:
        return TREND_FALLING
    return TREND_STEADY


@dataclass
class _Accumulator:
    packets: int = 0
    airtime_s: float = 0.0
    direct: int = 0
    relayed: int = 0
    via_mqtt: int = 0
    snr: list[float] = field(default_factory=list)
    rssi: list[int] = field(default_factory=list)
    #: SNR readings paired with their arrival time, so the trend can be taken in
    #: time order rather than ingest order without rescanning every packet.
    snr_samples: list[tuple[datetime, float]] = field(default_factory=list)
    portnums: Counter = field(default_factory=Counter)
    first_seen: datetime | None = None
    last_heard: datetime | None = None
    unfamiliar: bool = False
    unsized: int = 0


def analyse_packets(
    packets: Sequence[NetworkPacket],
    params: ModemParams,
    *,
    labels: Mapping[int, str] | None = None,
    known_nodes: Collection[int] = (),
    params_label: str = "",
) -> IntelReport:
    """Build per-node profiles from a window of packets.

    `known_nodes` is what the mesh already had a record of. Anything heard that
    is not in it is flagged as unfamiliar — see `NodeIntel.is_foreign`.

    Packets with no `sender_num` cannot be attributed to a node and are counted
    separately rather than dropped silently. Packets with no `payload_size`
    contribute no airtime, and are likewise counted so the totals can be read as
    the undercounts they are.
    """
    known = set(known_nodes)
    names = labels or {}
    accumulators: dict[int, _Accumulator] = {}
    unattributed = 0
    unsized = 0
    portnum_totals: Counter = Counter()

    for packet in packets:
        if packet.sender_num is None:
            unattributed += 1
            continue

        record = accumulators.setdefault(packet.sender_num, _Accumulator())
        record.packets += 1
        record.portnums[packet.portnum_name] += 1
        portnum_totals[packet.portnum_name] += 1

        if packet.payload_size is None:
            unsized += 1
            record.unsized += 1
        else:
            record.airtime_s += airtime_seconds(packet.payload_size, params)

        if packet.is_direct_rf:
            record.direct += 1
        elif packet.via_mqtt:
            record.via_mqtt += 1
        else:
            record.relayed += 1

        if packet.rx_snr is not None:
            record.snr.append(packet.rx_snr)
            record.snr_samples.append((packet.observed_at, packet.rx_snr))
        if packet.rx_rssi is not None:
            record.rssi.append(packet.rx_rssi)

        when = packet.observed_at
        if record.first_seen is None or when < record.first_seen:
            record.first_seen = when
        if record.last_heard is None or when > record.last_heard:
            record.last_heard = when

        if packet.sender_num not in known:
            record.unfamiliar = True

    timestamps = [packet.observed_at for packet in packets]
    window_s = (
        (max(timestamps) - min(timestamps)).total_seconds() if len(timestamps) > 1 else 0.0
    )
    total_airtime = sum(record.airtime_s for record in accumulators.values())

    nodes: list[NodeIntel] = []
    for node_num, record in accumulators.items():
        minutes = window_s / 60.0
        trend_input = [value for _, value in sorted(record.snr_samples, key=lambda item: item[0])]
        nodes.append(
            NodeIntel(
                node_num=node_num,
                label=names.get(node_num) or f"!{node_num:08x}",
                packets=record.packets,
                airtime_s=record.airtime_s,
                airtime_share=(
                    record.airtime_s / total_airtime if total_airtime > 0 else 0.0
                ),
                packets_per_minute=(record.packets / minutes if minutes > 0 else 0.0),
                direct_packets=record.direct,
                relayed_packets=record.relayed,
                via_mqtt_packets=record.via_mqtt,
                signal=compute_signal_stats(record.snr, record.rssi),
                snr_trend=_split_trend(trend_input),
                first_seen=record.first_seen,
                last_heard=record.last_heard,
                portnums=tuple(record.portnums.most_common()),
                is_unfamiliar=record.unfamiliar,
            )
        )

    # Busiest first: airtime is the scarce resource, so it is the ranking that
    # matters. The node number breaks ties so the order is stable between runs.
    nodes.sort(key=lambda node: (-node.airtime_s, node.node_num))

    return IntelReport(
        nodes=tuple(nodes),
        window_s=window_s,
        total_airtime_s=total_airtime,
        channel_duty_cycle=(
            duty_cycle(total_airtime, window_s) if window_s > 0 else 0.0
        ),
        analysed_packets=sum(record.packets for record in accumulators.values()),
        unsized_packets=unsized,
        unattributed_packets=unattributed,
        params_label=params_label,
        portnum_totals=tuple(portnum_totals.most_common()),
    )
