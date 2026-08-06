"""MeshChat – MonitorPage: the full Network Monitor dashboard."""
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from meshchat.analytics.packet_classifier import PORTNUM_TEXT, packet_category
from meshchat.utils.time_format import format_elapsed, relative_age
from meshchat.controllers.meshtastic_controller import ConnectionState
from meshchat.models.network_packet import NetworkPacket
from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.ui.monitor.dashboard_header import DashboardHeader
from meshchat.ui.monitor.distribution_panel import DistributionPanel
from meshchat.ui.monitor.filter_bar import FilterBar
from meshchat.ui.monitor.hop_panels import HopPanels
from meshchat.ui.monitor.metric_card import MetricCard, MultiValueCard
from meshchat.ui.monitor.node_inspector import NodeInspector
from meshchat.ui.monitor.packet_activity_chart import PacketActivityChart
from meshchat.ui.monitor.packet_log_view import PacketLogView
from meshchat.ui.monitor.rankings_panel import RankingsPanel
from meshchat.ui.map.map_widget import MapWidget

log = logging.getLogger(__name__)


def _matches_packet_type(pkt: NetworkPacket, type_filter: str) -> bool:
    """True if `pkt` belongs to the FilterBar `type_filter` category.

    Delegates to analytics.packet_classifier.packet_category() rather than
    re-deriving a portnum mapping, so this can't drift from
    DistributionPanel's PacketBreakdownPanel — both bucket "Unknown Port"
    and "Encrypted/Undecoded" into the one "Unknown/Encrypted" option/row.
    """
    category = packet_category(pkt.portnum, pkt.pki_encrypted)
    if type_filter == "Unknown/Encrypted":
        return category in ("Unknown Port", "Encrypted/Undecoded")
    return category == type_filter


class MonitorPage(QWidget):
    """
    Three-column Network Monitor dashboard.

    Left:   Map + packet activity chart + packet log
    Middle: Rankings panels
    Right:  KPI cards + hop panels + distribution panels + node inspector
    """

    # object, not int: see MapBridge.node_clicked — a real Meshtastic
    # node_num can exceed a signed 32-bit Qt int signal's range, and this
    # is fed directly from MapWidget.node_clicked (also object-typed).
    node_selected = Signal(object)   # node_num, for cross-page navigation

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header strip ───────────────────────────────────────────────
        self._header = DashboardHeader()
        layout.addWidget(self._header)

        # ── KPI card row ───────────────────────────────────────────────
        kpi_row = QWidget()
        kpi_row.setStyleSheet("background: #070D1F; border-bottom: 1px solid #1A2448;")
        kpi_layout = QHBoxLayout(kpi_row)
        kpi_layout.setContentsMargins(8, 6, 8, 6)
        kpi_layout.setSpacing(6)

        self._card_direct   = MetricCard("Direct",       "—",  tooltip="Unique nodes with a confirmed zero-hop RF packet")
        self._card_active   = MetricCard("Active",        "—",  tooltip="Unique nodes heard within the active time window (default 60 min)")
        self._card_total    = MetricCard("Total",         "—",  tooltip="All unique nodes seen in this session or NodeDB")
        self._card_ppm      = MetricCard("Pkt / min",     "—",  tooltip="Rolling 60-second packet count")
        self._card_pph      = MetricCard("Pkt / hr",      "—",  tooltip="Rolling 3600-second packet count")
        self._card_signal   = MultiValueCard("Signal",    ["SNR", "RSSI", "Noise"],
                                            tooltip="Median signal from direct non-MQTT RF packets")
        self._card_session  = MetricCard("Session pkts",  "0",  tooltip="Total deduplicated packets this session")
        self._card_elapsed  = MetricCard("Elapsed",       "0s", tooltip="Time since session start")
        self._card_infra    = MetricCard("Infrastructure","—",  tooltip="ROUTER / ROUTER_LATE / CLIENT_BASE")
        self._card_clients  = MetricCard("Clients",       "—",  tooltip="CLIENT / CLIENT_MUTE / TRACKER / SENSOR etc.")
        self._card_closest  = MetricCard("Closest",       "—",  tooltip="Closest positioned node from local radio")
        self._card_farthest = MetricCard("Farthest",      "—",  tooltip="Farthest positioned node from local radio")

        for card in [
            self._card_direct, self._card_active, self._card_total,
            self._card_ppm, self._card_pph, self._card_signal,
            self._card_session, self._card_elapsed,
            self._card_infra, self._card_clients,
            self._card_closest, self._card_farthest,
        ]:
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            kpi_layout.addWidget(card)

        layout.addWidget(kpi_row)

        # ── Filter bar ─────────────────────────────────────────────────
        self._filter_bar = FilterBar()
        self._filter_bar.filter_changed.connect(self._on_filter_changed)
        layout.addWidget(self._filter_bar)

        # ── Three-column splitter ──────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        layout.addWidget(splitter, 1)

        # ── Left column: Map + Chart + Log ─────────────────────────────
        left_col = QWidget()
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self._map_widget = MapWidget()
        self._map_widget.setMinimumHeight(200)
        self._map_widget.node_clicked.connect(self._on_ranking_node_selected)
        left_layout.addWidget(self._map_widget, 2)

        self._chart = PacketActivityChart()
        self._chart.setMinimumHeight(120)
        left_layout.addWidget(self._chart, 1)

        self._pkt_log = PacketLogView()
        self._pkt_log.setMinimumHeight(100)
        left_layout.addWidget(self._pkt_log, 1)

        splitter.addWidget(left_col)

        # ── Middle column: Rankings ────────────────────────────────────
        mid_col = QWidget()
        mid_layout = QVBoxLayout(mid_col)
        mid_layout.setContentsMargins(4, 4, 4, 4)
        mid_layout.setSpacing(4)

        self._rank_last_heard = RankingsPanel("LAST HEARD", "OLDEST HEARD")
        self._rank_most_pkts  = RankingsPanel("MOST PACKETS", "FEWEST PACKETS")
        self._rank_nearby     = RankingsPanel("NEARBY", "FARTHEST")
        self._rank_signal     = RankingsPanel("STRONGEST DIRECT", "WEAKEST DIRECT")
        self._rank_msgs       = RankingsPanel("MESSAGES SENT", "FEWEST MESSAGES")

        self._rank_panels = [
            self._rank_last_heard,
            self._rank_most_pkts,
            self._rank_nearby,
            self._rank_signal,
            self._rank_msgs,
        ]
        for panel in self._rank_panels:
            panel.node_selected.connect(self._on_ranking_node_selected)
            mid_layout.addWidget(panel, 1)

        splitter.addWidget(mid_col)

        # ── Right column: Hop + Dist + Inspector (scrollable) ──────────
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        right_col = QWidget()
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(6)

        self._inspector = NodeInspector()
        self._inspector.setMinimumHeight(300)
        self._inspector.show_on_map.connect(self.focus_node_on_map)
        right_layout.addWidget(self._inspector)

        self._hop_panels = HopPanels()
        right_layout.addWidget(self._hop_panels)

        self._dist_panel = DistributionPanel()
        right_layout.addWidget(self._dist_panel)

        right_layout.addStretch()
        right_scroll.setWidget(right_col)
        splitter.addWidget(right_scroll)

        # Initial column widths (52 / 22 / 26 %)
        splitter.setSizes([520, 220, 260])

        # ── State ──────────────────────────────────────────────────────
        self._session_start: datetime | None = None
        self._session_pkts = 0
        self._nodes: dict[int, NodeSnapshot] = {}
        # deque with maxlen evicts in O(1) rather than re-slicing a 10k list
        self._recent_packets: deque[NetworkPacket] = deque(maxlen=10_000)
        self._active_filter: dict = self._filter_bar.current_filter()
        self._local_node_num: int | None = None
        self._map_has_nodes = False
        self._positions: dict[int, tuple[float, float]] = {}  # node_num -> (lat, lon)
        self._channel_names: dict[int, str] = {}
        self._selected_node_num: int | None = None

        # 2-second refresh for rankings
        self._rank_timer = QTimer(self)
        self._rank_timer.timeout.connect(self._refresh_rankings)
        self._rank_timer.start(2000)

        # 1-second elapsed ticker
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._elapsed_timer.start(1000)

    # ------------------------------------------------------------------
    # Public API (called by MainWindow on signals from controller/ingestor)
    # ------------------------------------------------------------------

    def set_connection_state(self, state: ConnectionState, name: str = "") -> None:
        self._header.set_connection(state, name)
        if state == ConnectionState.CONNECTED and self._session_start is None:
            self._session_start = datetime.now(timezone.utc)

    def on_packet_ingested(self, pkt: NetworkPacket) -> None:
        if self._header.is_paused:
            return
        self._session_pkts += 1
        self._recent_packets.append(pkt)

        self._chart.record_packet(pkt.portnum)
        self._pkt_log.add_packet(pkt)
        self._header.mark_updated()
        self._card_session.set_value(str(self._session_pkts))

    def on_node_updated(self, node: NodeSnapshot) -> None:
        self._nodes[node.node_num] = node

    def on_stats_updated(self, ppm: int, pph: int) -> None:
        self._card_ppm.set_value(str(ppm))
        self._card_pph.set_value(str(pph))

    def on_local_telemetry(self, temp_c, humidity, pressure_hpa) -> None:
        self._header.set_env_telemetry(temp_c, humidity, pressure_hpa)

    def set_local_node(self, node_num: int | None) -> None:
        self._local_node_num = node_num

    def set_radio_config(self, summary) -> None:
        self._header.set_radio_config(summary)

    def set_channels(self, channels: list) -> None:
        """channels: list[ChannelSummary]"""
        self._channel_names = {c.index: c.name for c in channels}

    def on_position_updated(self, sample) -> None:
        if sample.latitude is None or sample.longitude is None:
            return
        self._positions[sample.node_num] = (sample.latitude, sample.longitude)
        node = self._nodes.get(sample.node_num)
        name = node.display_name if node else f"!{sample.node_num:08x}"
        role = (node.role or "") if node else ""
        is_local = sample.node_num == self._local_node_num
        last_heard_s = None
        if node and node.last_heard:
            last_heard_s = int((datetime.now(timezone.utc) - node.last_heard).total_seconds())
        self._map_widget.update_node(
            sample.node_num, sample.latitude, sample.longitude,
            name, role, is_local,
            node.last_hops_used if node else None,
            last_heard_s,
        )
        self._map_has_nodes = True

    def fit_map(self) -> None:
        if self._map_has_nodes:
            self._map_widget.fit_bounds()

    def clear_map(self) -> None:
        self._map_widget.clear_nodes()
        self._map_has_nodes = False
        self._set_selected_node(None)

    def focus_node_on_map(self, node_num: int) -> None:
        if node_num in self._positions:
            self._map_widget.focus_node(node_num)
        else:
            log.info("No known position for node %s — cannot focus map", node_num)
        # "Show on map" is a selection too — without this, the highlight
        # ring/ranking-row highlight would stay on whatever was selected
        # before, disagreeing with what the inspector now shows.
        self._on_ranking_node_selected(node_num)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _refresh_rankings(self) -> None:
        from meshchat.analytics.distance import haversine_km, format_distance

        all_nodes = list(self._nodes.values())
        nodes = all_nodes
        pkts: Sequence[NetworkPacket] = self._recent_packets
        now = datetime.now(timezone.utc)
        _ROW_LIMIT = 50

        age_s = self._active_filter.get("age_s")
        source_filter = self._active_filter.get("source", "All observed")
        type_filter = self._active_filter.get("portnum", "All")

        # Age scopes Last Heard / role / hardware / Active to the window —
        # but NOT Total or Direct below, which `all_nodes` (unfiltered)
        # stays around for: Total is "all unique nodes seen this session or
        # NodeDB" per the addendum spec, not "seen within the selected
        # window", and Direct matches that same always-unfiltered scope.
        # Source/type only make sense per-packet, so they narrow `pkts`
        # alone.
        if age_s is not None:
            cutoff = now - timedelta(seconds=age_s)
            nodes = [n for n in nodes if n.last_heard is not None and n.last_heard >= cutoff]
            pkts = [p for p in pkts if p.observed_at >= cutoff]

        if source_filter == "RF only":
            pkts = [p for p in pkts if p.via_mqtt is False]
        elif source_filter == "MQTT-path":
            pkts = [p for p in pkts if p.via_mqtt is True]
        elif source_filter == "Unknown transport":
            pkts = [p for p in pkts if p.via_mqtt is None]

        if type_filter != "All":
            pkts = [p for p in pkts if _matches_packet_type(p, type_filter)]

        # Last Heard
        # Bind to a local so the sort key is provably non-None: sorting a
        # datetime | None raises TypeError if a None ever slips through.
        heard_nodes = [(n, n.last_heard) for n in nodes if n.last_heard is not None]
        heard = [
            n for n, _ in sorted(
                heard_nodes,
                key=lambda pair: pair[1],
                reverse=not self._rank_last_heard.is_flipped(),
            )
        ]
        self._rank_last_heard.update_rows([
            (n.node_num, n.display_name, n.short_display,
             relative_age(n.last_heard))
            for n in heard[:_ROW_LIMIT]
        ])

        # One pass over the packet buffer for every per-sender statistic below.
        # This runs on a 2-second timer over up to 10k packets, and previously
        # made four separate full passes (counts, SNR, text counts, direct RF).
        pkt_counts: dict[int, int] = {}
        direct_snr: dict[int, float] = {}
        msg_counts: dict[int, int] = {}
        direct_snrs: list[float] = []
        direct_rssis: list[int] = []
        for p in pkts:
            sender = p.sender_num
            is_direct = p.is_direct_rf
            if is_direct:
                # Sampled independently: a direct packet can carry RSSI without
                # SNR, and previously such packets were dropped from the RSSI
                # median entirely because RSSI was nested under the SNR check.
                if p.rx_snr is not None:
                    direct_snrs.append(p.rx_snr)
                if p.rx_rssi is not None:
                    direct_rssis.append(p.rx_rssi)
            # `is None`, not truthiness — node number 0 is a legitimate sender
            # and the ingestor already tracks it. A falsy check silently
            # excluded it from every ranking.
            if sender is None:
                continue
            pkt_counts[sender] = pkt_counts.get(sender, 0) + 1
            if p.portnum == PORTNUM_TEXT:
                msg_counts[sender] = msg_counts.get(sender, 0) + 1
            if is_direct and p.rx_snr is not None:
                best = direct_snr.get(sender)
                if best is None or p.rx_snr > best:
                    direct_snr[sender] = p.rx_snr

        # Most Packets
        sorted_pkts = sorted(pkt_counts.items(), key=lambda x: x[1],
                             reverse=not self._rank_most_pkts.is_flipped())
        total_pkts = sum(pkt_counts.values()) or 1
        self._rank_most_pkts.update_rows([
            (num, self._node_name(num), "", f"{cnt}  ({int(cnt * 100 / total_pkts)}%)")
            for num, cnt in sorted_pkts[:_ROW_LIMIT]
        ])

        # Nearby / Farthest — needs our own position plus each node's
        # position. Restricted to node nums still in the (age-filtered)
        # `nodes` set: `self._positions` has no timestamp of its own, so a
        # node's last_heard (already applied above) is the only freshness
        # signal available — without this, Age had no effect here at all.
        scoped_node_nums = {n.node_num for n in nodes}
        local_pos = self._positions.get(self._local_node_num) if self._local_node_num else None
        if local_pos:
            lat0, lon0 = local_pos
            distances = [
                (num, haversine_km(lat0, lon0, lat, lon))
                for num, (lat, lon) in self._positions.items()
                if num != self._local_node_num and num in scoped_node_nums
            ]
            distances.sort(key=lambda x: x[1], reverse=self._rank_nearby.is_flipped())
            self._rank_nearby.update_rows([
                (num, self._node_name(num), "", format_distance(km))
                for num, km in distances[:_ROW_LIMIT]
            ])
            if distances:
                closest = min(distances, key=lambda x: x[1])
                farthest = max(distances, key=lambda x: x[1])
                self._card_closest.set_value(format_distance(closest[1]))
                self._card_farthest.set_value(format_distance(farthest[1]))
            else:
                # Same class of bug as Signal above: age-scoping can empty
                # `distances` on a later refresh (e.g. every positioned node
                # aged out) while a previous refresh's values would
                # otherwise stick around looking current.
                self._card_closest.set_value("—")
                self._card_farthest.set_value("—")
        else:
            self._rank_nearby.update_rows([])
            self._card_closest.set_value("—")
            self._card_farthest.set_value("—")

        # Strongest / Weakest Direct — best SNR per node (gathered above)
        sorted_snr = sorted(direct_snr.items(), key=lambda x: x[1],
                            reverse=not self._rank_signal.is_flipped())
        self._rank_signal.update_rows([
            (num, self._node_name(num), "", f"{snr:.1f} dB")
            for num, snr in sorted_snr[:_ROW_LIMIT]
        ])

        # Messages Sent — text packet count per node (gathered above)
        sorted_msgs = sorted(msg_counts.items(), key=lambda x: x[1],
                             reverse=not self._rank_msgs.is_flipped())
        self._rank_msgs.update_rows([
            (num, self._node_name(num), "", str(cnt))
            for num, cnt in sorted_msgs[:_ROW_LIMIT]
        ])

        # Node counts. Direct and Total intentionally read `all_nodes`, not
        # the age-filtered `nodes` — see the comment above. is_direct (not
        # a re-derived hop check) so this can't drift from the Nodes
        # table's own "Direct" column, which already accounts for
        # MQTT-bridged zero-hop packets not being a real direct RF contact.
        direct_nodes = {n.node_num for n in all_nodes if n.is_direct}
        active_nodes = {
            n.node_num for n in nodes
            if n.last_heard and (now - n.last_heard).total_seconds() <= (age_s or 3600)
        }
        self._card_direct.set_value(str(len(direct_nodes)))
        self._card_active.set_value(str(len(active_nodes)))
        self._card_total.set_value(str(len(all_nodes)))

        # Roles
        infra_roles = {"ROUTER", "ROUTER_LATE", "CLIENT_BASE", "ROUTER_CLIENT", "REPEATER"}
        infra = sum(1 for n in nodes if (n.role or "").upper() in infra_roles)
        clients = len(nodes) - infra
        self._card_infra.set_value(str(infra))
        self._card_clients.set_value(str(clients))

        # Signal — median of the direct-RF samples gathered in the pass above.
        # Guarded independently so an RSSI-only sample set still updates the
        # RSSI field instead of being suppressed by an empty SNR list. Both
        # branches always set a value (never skip) — a Source/Type filter
        # (e.g. "MQTT-path") can legitimately empty the direct-RF sample set
        # on a later refresh, and without an explicit "—" here the previous
        # refresh's median would keep showing as if it were still current.
        if direct_snrs:
            snrs = sorted(direct_snrs)
            self._card_signal.set_field("SNR", f"{snrs[len(snrs) // 2]:.1f} dB")
        else:
            self._card_signal.set_field("SNR", "—")
        if direct_rssis:
            rssis = sorted(direct_rssis)
            med_rssi = rssis[len(rssis) // 2]
            self._card_signal.set_field("RSSI", f"{med_rssi} dBm")
        else:
            self._card_signal.set_field("RSSI", "—")
        self._card_signal.set_field("Noise", "N/A")

        # Hop analytics
        self._hop_panels.update_data(pkts)

        # Distributions
        self._dist_panel.breakdown.update_packets(pkts)
        self._dist_panel.channels.update_data(pkts, self._channel_names)
        self._dist_panel.roles.update_nodes(nodes)
        self._dist_panel.hardware.update_nodes(nodes)

    def _tick_elapsed(self) -> None:
        if self._session_start is None:
            return
        delta = (datetime.now(timezone.utc) - self._session_start).total_seconds()
        self._card_elapsed.set_value(format_elapsed(int(delta)))

    def _on_filter_changed(self, f: dict) -> None:
        self._active_filter = f
        self._refresh_rankings()

    def _on_ranking_node_selected(self, node_num: int) -> None:
        node = self._nodes.get(node_num)
        if node:
            self._inspector.show_node(node)
        self._set_selected_node(node_num)
        self.node_selected.emit(node_num)

    def _set_selected_node(self, node_num: int | None) -> None:
        """Highlight node_num everywhere it can appear on this page — the
        map pin, and whichever ranking panel row currently represents it —
        so a click anywhere always shows which node is now selected."""
        self._selected_node_num = node_num
        self._map_widget.set_selected_node(node_num)
        for panel in self._rank_panels:
            panel.set_selected_node(node_num)

    def _node_name(self, num: int) -> str:
        n = self._nodes.get(num)
        if n:
            return n.display_name
        return f"!{num:08x}"


