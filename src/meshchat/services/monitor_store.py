"""MeshChat – services: SQLite-backed monitor data store with WAL mode."""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import platformdirs

from meshchat.database.schema import apply_schema
from meshchat.models.network_packet import NetworkPacket
from meshchat.models.network_session import NetworkSession
from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.models.position_sample import PositionSample
from meshchat.models.telemetry_sample import TelemetrySample

log = logging.getLogger(__name__)
APP_NAME = "MeshChat"

#: Days of packet/position/telemetry history to keep. Node identities and
#: chat messages are never pruned.
DEFAULT_RETAIN_DAYS = 30


def _db_path() -> Path:
    d = Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d / "monitor.db"


def _dt_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


class MonitorStore:
    """
    SQLite persistence for the Network Monitor.

    All writes are serialized through a dedicated background thread.
    Reads are performed on the calling thread using a separate connection.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or _db_path()

        # Apply schema synchronously before anything can read from this store —
        # callers may read (e.g. chat history) immediately after construction,
        # before the writer thread below would otherwise get around to it.
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        apply_schema(conn)
        conn.close()

        self._write_q: queue.Queue[Any] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True, name="db-writer")
        self._writer.start()
        self._available = True
        log.info("MonitorStore opened: %s", self._path)

    # ------------------------------------------------------------------
    # Public write API (non-blocking, queued)
    # ------------------------------------------------------------------

    def save_packet(self, pkt: NetworkPacket) -> None:
        self._enqueue(("packet", pkt))

    def save_session(self, session: NetworkSession) -> None:
        self._enqueue(("session", session))

    def save_position(self, pos: PositionSample) -> None:
        self._enqueue(("position", pos))

    def save_telemetry(self, tel: TelemetrySample) -> None:
        self._enqueue(("telemetry", tel))

    def upsert_node(self, node: NodeSnapshot) -> None:
        self._enqueue(("node", node))

    def save_message(self, msg) -> None:
        """Persist a ChatMessage (channel broadcast or direct message)."""
        self._enqueue(("message", msg))

    def _enqueue(self, item: Any) -> None:
        try:
            self._write_q.put_nowait(item)
        except queue.Full:
            log.warning("MonitorStore write queue full, dropping item")

    # ------------------------------------------------------------------
    # Read API (called from GUI/worker threads, opens own connection)
    # ------------------------------------------------------------------

    def read_packets(
        self,
        session_id: str,
        limit: int = 20_000,
        portnum: int | None = None,
    ) -> list[dict]:
        try:
            conn = self._read_conn()
            q = "SELECT * FROM packets WHERE session_id=?"
            args: list[Any] = [session_id]
            if portnum is not None:
                q += " AND portnum=?"
                args.append(portnum)
            q += " ORDER BY observed_at DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(q, args).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.error("MonitorStore.read_packets failed: %s", exc)
            return []

    def packet_count(self, session_id: str) -> int:
        try:
            conn = self._read_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM packets WHERE session_id=?", (session_id,)
            ).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception as exc:
            log.error("MonitorStore.packet_count failed: %s", exc)
            return 0

    def read_nodes(self) -> list[dict]:
        try:
            conn = self._read_conn()
            rows = conn.execute("SELECT * FROM nodes ORDER BY last_heard DESC").fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.error("MonitorStore.read_nodes failed: %s", exc)
            return []

    def read_latest_positions(self) -> dict[int, dict]:
        """Latest known position per node, across all history — used to
        populate the map with every previously-seen node on startup, before
        any radio is connected this session (mirrors the official Meshtastic
        app always showing its saved NodeDB positions)."""
        try:
            conn = self._read_conn()
            rows = conn.execute(
                """SELECT p.* FROM positions p
                INNER JOIN (
                    SELECT node_num, MAX(observed_at) AS max_obs
                    FROM positions
                    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                    GROUP BY node_num
                ) latest
                ON p.node_num = latest.node_num AND p.observed_at = latest.max_obs"""
            ).fetchall()
            conn.close()
            return {r["node_num"]: dict(r) for r in rows}
        except Exception as exc:
            log.error("MonitorStore.read_latest_positions failed: %s", exc)
            return {}

    def read_positions(self, node_num: int, limit: int = 1000) -> list[dict]:
        try:
            conn = self._read_conn()
            rows = conn.execute(
                "SELECT * FROM positions WHERE node_num=? ORDER BY observed_at DESC LIMIT ?",
                (node_num, limit),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.error("MonitorStore.read_positions failed: %s", exc)
            return []

    def read_latest_position(self, node_num: int) -> dict | None:
        rows = self.read_positions(node_num, limit=1)
        return rows[0] if rows else None

    def read_messages(self, limit: int = 5000) -> list[dict]:
        """All persisted chat messages (channel broadcasts and direct messages),
        oldest first, across all sessions — chat history survives app restarts."""
        try:
            conn = self._read_conn()
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY observed_at ASC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.error("MonitorStore.read_messages failed: %s", exc)
            return []

    def prune(self, retain_days: int = DEFAULT_RETAIN_DAYS) -> dict[str, int]:
        """Delete high-volume rows older than `retain_days`.

        Packets, positions, and telemetry accumulate forever otherwise — a
        monitor left running writes them continuously, and nothing ever
        removed them. `nodes` and `messages` are deliberately exempt: the node
        database is the app's memory of the mesh, and chat history is user
        content that must not be silently discarded.

        Returns rows deleted per table. Safe to call with retain_days <= 0,
        which is treated as "keep everything".
        """
        if retain_days <= 0:
            return {}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
        deleted: dict[str, int] = {}
        try:
            conn = self._read_conn()
            with conn:
                for table in ("packets", "positions", "telemetry"):
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE observed_at < ?", (cutoff,)  # noqa: S608
                    )
                    if cur.rowcount > 0:
                        deleted[table] = cur.rowcount
            conn.close()
            if deleted:
                total = sum(deleted.values())
                log.info("Pruned %d row(s) older than %d days: %s",
                         total, retain_days, deleted)
        except Exception as exc:
            log.error("MonitorStore.prune failed: %s", exc)
        return deleted

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._write_q.put(None)  # sentinel
        self._writer.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Private writer loop
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        try:
            conn = sqlite3.connect(str(self._path), check_same_thread=True)
            conn.row_factory = sqlite3.Row
            apply_schema(conn)
            batch: list[Any] = []
            while not self._stop.is_set() or not self._write_q.empty():
                try:
                    item = self._write_q.get(timeout=0.1)
                    if item is None:
                        break
                    batch.append(item)
                    if len(batch) >= 100:
                        self._flush(conn, batch)
                        batch.clear()
                except queue.Empty:
                    if batch:
                        self._flush(conn, batch)
                        batch.clear()
            if batch:
                self._flush(conn, batch)
            conn.close()
        except Exception as exc:
            log.error("MonitorStore writer crashed: %s", exc, exc_info=True)
            self._available = False

    def _flush(self, conn: sqlite3.Connection, batch: list) -> None:
        try:
            with conn:
                for kind, obj in batch:
                    if kind == "packet":
                        self._write_packet(conn, obj)
                    elif kind == "session":
                        self._write_session(conn, obj)
                    elif kind == "position":
                        self._write_position(conn, obj)
                    elif kind == "telemetry":
                        self._write_telemetry(conn, obj)
                    elif kind == "node":
                        self._write_node(conn, obj)
                    elif kind == "message":
                        self._write_message(conn, obj)
        except Exception as exc:
            log.error("MonitorStore flush error: %s", exc)

    def _write_packet(self, conn: sqlite3.Connection, pkt: NetworkPacket) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO packets
            (session_id, observed_at, rx_time, sender_num, sender_id,
             destination_num, packet_id, channel_index, portnum, portnum_name,
             payload_size, rx_snr, rx_rssi, hop_start, hop_limit, hops_used,
             via_mqtt, transport_mechanism, pki_encrypted, want_ack, priority)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pkt.session_id, _dt_str(pkt.observed_at), _dt_str(pkt.rx_time),
                pkt.sender_num, pkt.sender_id, pkt.destination_num, pkt.packet_id,
                pkt.channel_index, pkt.portnum, pkt.portnum_name, pkt.payload_size,
                pkt.rx_snr, pkt.rx_rssi, pkt.hop_start, pkt.hop_limit, pkt.hops_used,
                int(pkt.via_mqtt) if pkt.via_mqtt is not None else None,
                pkt.transport_mechanism,
                int(pkt.pki_encrypted) if pkt.pki_encrypted is not None else None,
                int(pkt.want_ack) if pkt.want_ack is not None else None,
                pkt.priority,
            ),
        )

    def _write_session(self, conn: sqlite3.Connection, s: NetworkSession) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO sessions
            (id, started_at, ended_at, local_node_num, local_node_id,
             transport, connection_target, meshtastic_version, firmware_version)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                s.id, _dt_str(s.started_at), _dt_str(s.ended_at),
                s.local_node_num, s.local_node_id, s.transport,
                s.connection_target, s.meshtastic_version, s.firmware_version,
            ),
        )

    def _write_position(self, conn: sqlite3.Connection, pos: PositionSample) -> None:
        conn.execute(
            """INSERT INTO positions
            (node_num, observed_at, latitude, longitude, altitude_m,
             speed_ms, heading_deg, position_time, precision_bits)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                pos.node_num, _dt_str(pos.observed_at),
                pos.latitude, pos.longitude, pos.altitude_m,
                pos.speed_ms, pos.heading_deg, _dt_str(pos.position_time),
                pos.precision_bits,
            ),
        )

    def _write_telemetry(self, conn: sqlite3.Connection, tel: TelemetrySample) -> None:
        conn.execute(
            """INSERT INTO telemetry
            (node_num, observed_at, battery_level, voltage, channel_utilization,
             air_util_tx, temperature_c, relative_humidity,
             barometric_pressure_hpa, gas_resistance_ohm)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                tel.node_num, _dt_str(tel.observed_at),
                tel.battery_level, tel.voltage, tel.channel_utilization,
                tel.air_util_tx, tel.temperature_c, tel.relative_humidity,
                tel.barometric_pressure_hpa, tel.gas_resistance_ohm,
            ),
        )

    def _write_node(self, conn: sqlite3.Connection, node: NodeSnapshot) -> None:
        conn.execute(
            """INSERT INTO nodes
            (node_num, node_id, long_name, short_name, role, hw_model,
             first_seen, last_heard, packet_count, text_count)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(node_num) DO UPDATE SET
                node_id=excluded.node_id,
                long_name=COALESCE(excluded.long_name, long_name),
                short_name=COALESCE(excluded.short_name, short_name),
                role=COALESCE(excluded.role, role),
                hw_model=COALESCE(excluded.hw_model, hw_model),
                last_heard=excluded.last_heard,
                packet_count=excluded.packet_count,
                text_count=excluded.text_count""",
            (
                node.node_num, node.node_id, node.long_name, node.short_name,
                node.role, node.hw_model,
                _dt_str(node.first_seen), _dt_str(node.last_heard),
                node.packet_count, node.text_count,
            ),
        )

    def _write_message(self, conn: sqlite3.Connection, msg) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO messages
            (session_id, local_id, packet_id, channel_index, destination_num,
             direction, sender_num, sender_id, sender_name, text,
             observed_at, byte_count, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "", msg.local_id, msg.packet_id, msg.channel_index, msg.destination_num,
                msg.direction.value, msg.sender_num, msg.sender_id, msg.sender_name, msg.text,
                _dt_str(msg.timestamp), len(msg.text.encode("utf-8")), msg.status.value,
            ),
        )

    def _read_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
