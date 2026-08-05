"""MeshChat – services: canonical packet ingestion pipeline."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from threading import Lock

from PySide6.QtCore import QObject, Signal

from meshchat.analytics.hop_metrics import compute_hops_used
from meshchat.analytics.packet_classifier import (
    PORTNUM_POSITION,
    PORTNUM_TELEMETRY,
    PORTNUM_TEXT,
    classify_portnum,
)
from meshchat.analytics.rolling_rates import DualRateTracker
from meshchat.models.network_packet import NetworkPacket
from meshchat.models.network_session import NetworkSession
from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.models.position_sample import PositionSample
from meshchat.models.telemetry_sample import TelemetrySample

log = logging.getLogger(__name__)

_DEDUP_TTL_S = 120  # 2-minute dedup window


def _copy_node_snapshot(node: NodeSnapshot) -> NodeSnapshot:
    """Return an immutable-in-spirit copy of a live NodeSnapshot to emit over
    a signal, so consumers on other threads never see a partially-mutated
    object mid-update."""
    return NodeSnapshot(
        node_num=node.node_num,
        node_id=node.node_id,
        long_name=node.long_name,
        short_name=node.short_name,
        role=node.role,
        hw_model=node.hw_model,
        first_seen=node.first_seen,
        last_heard=node.last_heard,
        packet_count=node.packet_count,
        text_count=node.text_count,
        position_count=node.position_count,
        telemetry_count=node.telemetry_count,
        last_snr=node.last_snr,
        last_rssi=node.last_rssi,
        last_hops_used=node.last_hops_used,
        last_hop_start=node.last_hop_start,
        via_mqtt_count=node.via_mqtt_count,
        rf_count=node.rf_count,
        last_telemetry_at=node.last_telemetry_at,
        battery_level=node.battery_level,
        voltage=node.voltage,
        channel_utilization=node.channel_utilization,
        air_util_tx=node.air_util_tx,
        temperature_c=node.temperature_c,
        relative_humidity=node.relative_humidity,
        barometric_pressure_hpa=node.barometric_pressure_hpa,
    )


def _get(d: dict, *keys, default=None):
    for k in keys:
        if k in d:
            return d[k]
    return default


def _parse_dt(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return None


def _dedup_key(pkt: dict) -> str | None:
    sender = _get(pkt, "from", "fromNum")
    pid = pkt.get("id")
    decoded = pkt.get("decoded", {})
    portnum = _get(decoded, "portnum")
    channel = _get(pkt, "channel", "channelIndex") or 0

    # pid is not None, not truthy `and pid`: a packet id of 0 is a
    # legitimate value (same node_num == 0 pattern fixed elsewhere in this
    # codebase) and is just as strong a dedup key as any other id — a
    # truthy check would wrongly fall through to the weaker, 5-second-
    # bucketed fallback fingerprint for it.
    if sender is not None and pid is not None:
        return f"{sender}:{pid}:{portnum}:{channel}"
    # Fallback fingerprint
    try:
        text = decoded.get("text", "") or ""
        payload = decoded.get("payload", b"") or b""
        raw = f"{sender}{portnum}{channel}{text}"
        raw_bytes = raw.encode() + (payload if isinstance(payload, bytes) else payload.encode())
        # monotonic, not time.time(): the TTL/expiry tracking below this
        # function (_is_duplicate) already uses time.monotonic(), which is
        # immune to wall-clock adjustments (NTP sync, DST, manual changes).
        # A clock jump mid-session could otherwise shift which 5-second
        # bucket a packet lands in, letting a genuine duplicate slip through.
        ts_bucket = int(time.monotonic() / 5) * 5  # 5-second bucket
        return hashlib.sha256(raw_bytes + str(ts_bucket).encode()).hexdigest()
    except Exception:
        return None


class PacketIngestor(QObject):
    """
    Canonical packet ingestion pipeline.

    Receives raw Meshtastic packet dicts (via Qt signals from controller),
    normalizes them into NetworkPacket, deduplicates, updates node snapshots,
    and emits typed signals for consumers.

    Threading: this object is constructed on, and stays on, the GUI thread —
    it is never moveToThread'd. The controller's worker emits `raw_packet`
    from its own thread and Qt queues delivery here. The locks below are
    therefore defensive rather than load-bearing today; they exist so that
    moving this to a worker thread later, or handing snapshots to the
    MonitorStore writer thread, does not silently introduce data races.
    (An earlier version of this docstring claimed the opposite. It was wrong.)
    """

    # Signals emitted to UI/consumers
    packet_ingested = Signal(object)          # NetworkPacket
    node_updated = Signal(object)             # NodeSnapshot
    position_updated = Signal(object)         # PositionSample
    telemetry_updated = Signal(object)        # TelemetrySample
    stats_updated = Signal(int, int)          # per_minute, per_hour
    nodedb_seeded = Signal()                  # emitted once seed_from_nodedb finishes a batch

    def __init__(
        self,
        session: NetworkSession,
        store=None,  # MonitorStore | None
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._store = store
        self._rate = DualRateTracker()

        # Dedup LRU cache: key -> expiry_monotonic
        self._dedup: OrderedDict[str, float] = OrderedDict()
        self._dedup_lock = Lock()

        # In-memory node snapshots
        self._nodes: dict[int, NodeSnapshot] = {}
        self._nodes_lock = Lock()

        # In-memory packet ring (last 10k)
        # deque with maxlen evicts in O(1); the previous list+slice rebuilt a
        # 10k-element list on every packet once the buffer was full.
        self._recent_max = 10_000
        self._recent_packets: deque[NetworkPacket] = deque(maxlen=self._recent_max)
        # Appended from the worker thread, snapshotted from the GUI thread
        # (CSV export). Copying a deque while it is being appended to can
        # raise "deque mutated during iteration", so both sides take this.
        self._recent_lock = Lock()

    def ingest_raw(self, raw: dict) -> None:
        """
        Entry point: normalize, deduplicate, and dispatch a raw packet dict.

        Connected to the controller's `raw_packet` signal, so Qt delivers it on
        this object's thread (the GUI thread). Never call it directly from a
        PubSub callback — those run on the radio worker thread.
        """
        key = _dedup_key(raw)
        if key and self._is_duplicate(key):
            return

        pkt = self._normalize(raw)
        if pkt is None:
            return

        # Update ring buffer
        with self._recent_lock:
            self._recent_packets.append(pkt)

        # Update session counter
        self._session.packet_count += 1

        # Rate tracking (monotonic)
        self._rate.record()

        # Update node snapshot
        self._update_node(pkt, raw)

        # Sub-type handling
        portnum = pkt.portnum
        if portnum == PORTNUM_POSITION:
            self._handle_position(raw, pkt)
        elif portnum == PORTNUM_TELEMETRY:
            self._handle_telemetry(raw, pkt)

        # Persist
        if self._store:
            self._store.save_packet(pkt)

        self.packet_ingested.emit(pkt)
        self.stats_updated.emit(self._rate.per_minute, self._rate.per_hour)

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _is_duplicate(self, key: str) -> bool:
        now = time.monotonic()
        with self._dedup_lock:
            self._prune_dedup(now)
            if key in self._dedup:
                return True
            self._dedup[key] = now + _DEDUP_TTL_S
            if len(self._dedup) > 5000:
                # Prune oldest half when too large
                for _ in range(2500):
                    self._dedup.popitem(last=False)
            return False

    def _prune_dedup(self, now: float) -> None:
        # Every entry gets the same fixed TTL and the OrderedDict is in
        # insertion order, so expiries are ascending — the first unexpired
        # entry means everything after it is unexpired too. Stopping there
        # keeps this O(expired) instead of O(cache size) on every packet.
        while self._dedup:
            key, expiry = next(iter(self._dedup.items()))
            if expiry > now:
                break
            del self._dedup[key]

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _normalize(self, raw: dict) -> NetworkPacket | None:
        try:
            decoded = raw.get("decoded", {}) or {}
            portnum = _get(decoded, "portnum")
            portnum_name = classify_portnum(portnum)

            sender_num = _get(raw, "from", "fromNum")
            sender_id = _get(raw, "fromId")

            hop_start = _get(raw, "hopStart", "hop_start")
            hop_limit = _get(raw, "hopLimit", "hop_limit")
            hops_used = compute_hops_used(hop_start, hop_limit)

            rx_ts = _get(raw, "rxTime", "rx_time")
            rx_time: datetime | None = None
            if rx_ts:
                try:
                    rx_time = datetime.fromtimestamp(float(rx_ts), tz=timezone.utc)
                except (OSError, OverflowError, ValueError):
                    pass

            text: str | None = None
            if portnum == PORTNUM_TEXT:
                text = decoded.get("text")
                if not isinstance(text, str):
                    pl = decoded.get("payload")
                    if isinstance(pl, bytes):
                        try:
                            text = pl.decode("utf-8")
                        except UnicodeDecodeError:
                            pass

            payload_size: int | None = None
            pl = decoded.get("payload")
            if isinstance(pl, bytes):
                payload_size = len(pl)
            elif text:
                payload_size = len(text.encode("utf-8"))

            via_mqtt_raw = _get(raw, "viaMqtt", "via_mqtt")
            via_mqtt = bool(via_mqtt_raw) if via_mqtt_raw is not None else None

            pki = _get(decoded, "pkiEncrypted", "pki_encrypted")

            # Minimal safe JSON metadata (no PSKs, no key material)
            safe_meta = {
                "id": raw.get("id"),
                "to": raw.get("to"),
                "channel": _get(raw, "channel"),
            }

            return NetworkPacket(
                session_id=self._session.id,
                observed_at=datetime.now(timezone.utc),
                rx_time=rx_time,
                sender_num=sender_num,
                sender_id=sender_id,
                destination_num=_get(raw, "to", "toNum"),
                packet_id=raw.get("id"),
                channel_index=int(_get(raw, "channel", "channelIndex") or 0),
                portnum=portnum,
                portnum_name=portnum_name,
                text=text,
                payload_size=payload_size,
                rx_snr=_get(raw, "rxSnr", "rx_snr"),
                rx_rssi=_get(raw, "rxRssi", "rx_rssi"),
                hop_start=hop_start,
                hop_limit=hop_limit,
                hops_used=hops_used,
                via_mqtt=via_mqtt,
                transport_mechanism=_get(raw, "transportMechanism", "transport_mechanism"),
                pki_encrypted=bool(pki) if pki is not None else None,
                want_ack=raw.get("wantAck"),
                priority=_get(decoded, "priority"),
                raw_metadata_json=json.dumps(safe_meta),
            )
        except Exception as exc:
            log.debug("PacketIngestor._normalize failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Node snapshot updates
    # ------------------------------------------------------------------

    def _update_node(self, pkt: NetworkPacket, raw: dict) -> None:
        if pkt.sender_num is None:
            return
        num = pkt.sender_num
        now = datetime.now(timezone.utc)

        with self._nodes_lock:
            node = self._nodes.get(num)
            if node is None:
                node = NodeSnapshot(
                    node_num=num,
                    node_id=pkt.sender_id,
                    first_seen=now,
                )
                self._nodes[num] = node

            node.last_heard = now
            node.packet_count += 1
            node.last_snr = pkt.rx_snr
            node.last_rssi = pkt.rx_rssi
            node.last_hops_used = pkt.hops_used
            node.last_hop_start = pkt.hop_start

            if pkt.is_via_mqtt:
                node.via_mqtt_count += 1
            else:
                node.rf_count += 1

            if pkt.portnum == PORTNUM_TEXT:
                node.text_count += 1
            elif pkt.portnum == PORTNUM_POSITION:
                node.position_count += 1
            elif pkt.portnum == PORTNUM_TELEMETRY:
                node.telemetry_count += 1

            # Try to update name/role from NodeInfo
            decoded = raw.get("decoded", {}) or {}
            user = decoded.get("user", {}) or {}
            if user:
                node.long_name = user.get("longName") or user.get("long_name") or node.long_name
                node.short_name = user.get("shortName") or user.get("short_name") or node.short_name
                node.role = user.get("role") or node.role
                node.hw_model = user.get("hwModel") or user.get("hw_model") or node.hw_model
                if not node.node_id:
                    node.node_id = user.get("id") or pkt.sender_id

            snap = _copy_node_snapshot(node)

        if self._store:
            self._store.upsert_node(snap)
        self.node_updated.emit(snap)

    def _handle_position(self, raw: dict, pkt: NetworkPacket) -> None:
        if pkt.sender_num is None:
            # positions.node_num is NOT NULL, so an anonymous position packet
            # would fail the insert and land on the map keyed by None.
            log.debug("Ignoring position packet with no sender")
            return
        decoded = raw.get("decoded", {}) or {}
        pos_data = decoded.get("position", {}) or {}
        if not pos_data:
            log.info(
                "Position packet from %s has no decoded position payload "
                "(likely on a channel this radio can't decrypt)",
                pkt.sender_id or pkt.sender_num,
            )
            return
        try:
            lat = pos_data.get("latitudeI")
            if lat is None:
                lat = pos_data.get("latitude_i")
            lon = pos_data.get("longitudeI")
            if lon is None:
                lon = pos_data.get("longitude_i")
            lat_f = lat / 1e7 if lat is not None else None
            lon_f = lon / 1e7 if lon is not None else None

            alt = pos_data.get("altitude")
            speed = pos_data.get("groundSpeed") or pos_data.get("ground_speed")
            heading = pos_data.get("groundTrack") or pos_data.get("ground_track")
            if heading is not None:
                heading = heading / 100.0  # convert to degrees

            sample = PositionSample(
                node_num=pkt.sender_num,
                observed_at=pkt.observed_at,
                latitude=lat_f,
                longitude=lon_f,
                altitude_m=float(alt) if alt is not None else None,
                speed_ms=float(speed) if speed is not None else None,
                heading_deg=float(heading) if heading is not None else None,
                precision_bits=pos_data.get("precisionBits") or pos_data.get("precision_bits"),
            )
            if sample.is_valid:
                if self._store:
                    self._store.save_position(sample)
                self.position_updated.emit(sample)
                log.info("Live position for node %s: %.5f, %.5f", pkt.sender_num, lat_f, lon_f)
            else:
                log.info("Discarded invalid position sample for node %s: lat=%s lon=%s",
                         pkt.sender_num, lat_f, lon_f)
        except Exception as exc:
            log.debug("Position parse error: %s", exc)

    def _handle_telemetry(self, raw: dict, pkt: NetworkPacket) -> None:
        if pkt.sender_num is None:
            # Same as positions: telemetry.node_num is NOT NULL.
            log.debug("Ignoring telemetry packet with no sender")
            return
        decoded = raw.get("decoded", {}) or {}
        tel_data = decoded.get("telemetry", {}) or {}
        if not tel_data:
            return
        try:
            dev = tel_data.get("deviceMetrics", {}) or tel_data.get("device_metrics", {}) or {}
            env = tel_data.get("environmentMetrics", {}) or tel_data.get("environment_metrics", {}) or {}

            def _f(d: dict, *keys) -> float | None:
                for k in keys:
                    v = d.get(k)
                    if v is not None:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
                return None

            sample = TelemetrySample(
                node_num=pkt.sender_num,
                observed_at=pkt.observed_at,
                battery_level=_f(dev, "batteryLevel", "battery_level"),
                voltage=_f(dev, "voltage"),
                channel_utilization=_f(dev, "channelUtilization", "channel_utilization"),
                air_util_tx=_f(dev, "airUtilTx", "air_util_tx"),
                temperature_c=_f(env, "temperature"),
                relative_humidity=_f(env, "relativeHumidity", "relative_humidity"),
                barometric_pressure_hpa=_f(env, "barometricPressure", "barometric_pressure"),
                gas_resistance_ohm=_f(env, "gasResistance", "gas_resistance"),
            )
            if self._store:
                self._store.save_telemetry(sample)
            self.telemetry_updated.emit(sample)
            self._merge_telemetry_into_node(sample)
        except Exception as exc:
            log.debug("Telemetry parse error: %s", exc)

    def _merge_telemetry_into_node(self, sample: TelemetrySample) -> None:
        """Store the latest telemetry reading on the node snapshot so it's
        visible in the Node Inspector — previously telemetry only reached a
        transient signal (telemetry_updated) with nothing subscribed to it."""
        if sample.node_num is None:
            return
        with self._nodes_lock:
            node = self._nodes.get(sample.node_num)
            if node is None:
                node = NodeSnapshot(node_num=sample.node_num, first_seen=sample.observed_at)
                self._nodes[sample.node_num] = node
            node.last_telemetry_at = sample.observed_at
            if sample.battery_level is not None:
                node.battery_level = sample.battery_level
            if sample.voltage is not None:
                node.voltage = sample.voltage
            if sample.channel_utilization is not None:
                node.channel_utilization = sample.channel_utilization
            if sample.air_util_tx is not None:
                node.air_util_tx = sample.air_util_tx
            if sample.temperature_c is not None:
                node.temperature_c = sample.temperature_c
            if sample.relative_humidity is not None:
                node.relative_humidity = sample.relative_humidity
            if sample.barometric_pressure_hpa is not None:
                node.barometric_pressure_hpa = sample.barometric_pressure_hpa
            snap = _copy_node_snapshot(node)
        if self._store:
            self._store.upsert_node(snap)
        self.node_updated.emit(snap)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_nodes(self) -> list[NodeSnapshot]:
        # Copies, not references. NodeSnapshot is a mutable dataclass that the
        # ingestion path keeps updating in place, so handing out the live
        # objects meant callers never actually got a snapshot — the node table
        # could re-sort by a last_heard that changed after it was read.
        with self._nodes_lock:
            return [_copy_node_snapshot(n) for n in self._nodes.values()]

    def get_node(self, node_num: int) -> NodeSnapshot | None:
        with self._nodes_lock:
            node = self._nodes.get(node_num)
            return _copy_node_snapshot(node) if node is not None else None

    def get_recent_packets(self) -> list[NetworkPacket]:
        with self._recent_lock:
            return list(self._recent_packets)

    def update_node_from_db(self, node_num: int, nodes_by_num: dict) -> None:
        """Merge NodeDB data into the in-memory snapshot."""
        if node_num not in nodes_by_num:
            return
        node_data = nodes_by_num[node_num]
        user = node_data.get("user", {}) or {}
        with self._nodes_lock:
            node = self._nodes.get(node_num)
            if node is None:
                node = NodeSnapshot(node_num=node_num)
                self._nodes[node_num] = node
            node.node_id = user.get("id") or node.node_id
            node.long_name = user.get("longName") or node.long_name
            node.short_name = user.get("shortName") or node.short_name
            node.role = user.get("role") or node.role
            node.hw_model = user.get("hwModel") or node.hw_model

    def seed_from_nodedb(self, raw_nodes: list[dict]) -> None:
        """
        Seed node snapshots (and positions) from the meshtastic library's
        already-known NodeDB, right after connecting — instead of waiting for
        each node to re-announce itself with a live packet this session.
        Must be called from the same thread this ingestor lives on.
        """
        now = datetime.now(timezone.utc)
        nodes_processed = 0
        positions_pushed = 0
        for node_data in raw_nodes:
            if not isinstance(node_data, dict):
                continue
            num = node_data.get("num")
            if num is None:
                continue
            nodes_processed += 1
            user = node_data.get("user", {}) or {}

            with self._nodes_lock:
                node = self._nodes.get(num)
                if node is None:
                    node = NodeSnapshot(node_num=num, first_seen=now)
                    self._nodes[num] = node
                node.node_id = user.get("id") or node.node_id
                node.long_name = user.get("longName") or node.long_name
                node.short_name = user.get("shortName") or node.short_name
                node.role = user.get("role") or node.role
                node.hw_model = user.get("hwModel") or node.hw_model

                last_heard_epoch = node_data.get("lastHeard")
                if last_heard_epoch and node.last_heard is None:
                    try:
                        node.last_heard = datetime.fromtimestamp(float(last_heard_epoch), tz=timezone.utc)
                    except (OSError, OverflowError, ValueError):
                        pass

                # The radio's NodeDB caches each node's last-known device
                # metrics too — seed them so the inspector shows telemetry
                # immediately on connect, not just after a fresh live packet.
                dev_metrics = node_data.get("deviceMetrics") or {}
                if dev_metrics.get("batteryLevel") is not None:
                    node.battery_level = dev_metrics["batteryLevel"]
                if dev_metrics.get("voltage") is not None:
                    node.voltage = dev_metrics["voltage"]
                if dev_metrics.get("channelUtilization") is not None:
                    node.channel_utilization = dev_metrics["channelUtilization"]
                if dev_metrics.get("airUtilTx") is not None:
                    node.air_util_tx = dev_metrics["airUtilTx"]

                snap = _copy_node_snapshot(node)

            if self._store:
                self._store.upsert_node(snap)
            self.node_updated.emit(snap)

            position = node_data.get("position") or {}
            lat = position.get("latitude")
            lon = position.get("longitude")
            if lat is not None and lon is not None and (lat != 0.0 or lon != 0.0):
                sample = PositionSample(
                    node_num=num,
                    observed_at=now,
                    latitude=float(lat),
                    longitude=float(lon),
                    altitude_m=float(position["altitude"]) if position.get("altitude") is not None else None,
                    speed_ms=None,
                    heading_deg=None,
                    precision_bits=position.get("precisionBits"),
                )
                if sample.is_valid:
                    if self._store:
                        self._store.save_position(sample)
                    self.position_updated.emit(sample)
                    positions_pushed += 1

        log.info(
            "NodeDB seed: processed %d node(s), pushed %d position(s) to the map",
            nodes_processed, positions_pushed,
        )
        self.nodedb_seeded.emit()

    def seed_from_store(self, node_rows: list[dict], positions_by_num: dict[int, dict]) -> None:
        """
        Populate node snapshots (and the map) from MonitorStore's persisted
        history at app startup — before any radio is connected this session.
        Mirrors the official Meshtastic app always showing its saved NodeDB,
        rather than starting from a blank slate every launch.
        """
        nodes_processed = 0
        positions_pushed = 0
        for row in node_rows:
            num = row.get("node_num")
            if num is None:
                continue
            nodes_processed += 1

            with self._nodes_lock:
                node = self._nodes.get(num)
                if node is None:
                    node = NodeSnapshot(node_num=num)
                    self._nodes[num] = node
                node.node_id = row.get("node_id") or node.node_id
                node.long_name = row.get("long_name") or node.long_name
                node.short_name = row.get("short_name") or node.short_name
                node.role = row.get("role") or node.role
                node.hw_model = row.get("hw_model") or node.hw_model
                node.first_seen = node.first_seen or _parse_dt(row.get("first_seen"))
                node.last_heard = node.last_heard or _parse_dt(row.get("last_heard"))
                node.packet_count = max(node.packet_count, row.get("packet_count") or 0)
                node.text_count = max(node.text_count, row.get("text_count") or 0)
                snap = _copy_node_snapshot(node)

            self.node_updated.emit(snap)

            pos_row = positions_by_num.get(num)
            if pos_row and pos_row.get("latitude") is not None and pos_row.get("longitude") is not None:
                sample = PositionSample(
                    node_num=num,
                    observed_at=_parse_dt(pos_row.get("observed_at")) or datetime.now(timezone.utc),
                    latitude=pos_row["latitude"],
                    longitude=pos_row["longitude"],
                    altitude_m=pos_row.get("altitude_m"),
                    speed_ms=pos_row.get("speed_ms"),
                    heading_deg=pos_row.get("heading_deg"),
                    precision_bits=pos_row.get("precision_bits"),
                )
                if sample.is_valid:
                    self.position_updated.emit(sample)
                    positions_pushed += 1

        log.info(
            "Startup seed from persisted history: %d node(s), %d position(s) pushed to the map",
            nodes_processed, positions_pushed,
        )
        self.nodedb_seeded.emit()
