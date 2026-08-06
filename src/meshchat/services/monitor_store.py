"""MeshChat – services: SQLite-backed monitor data store with WAL mode."""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
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

#: A single queued item gets this many extra attempts after a transient
#: SQLite error (lock contention, SQLITE_BUSY) before it's given up on as a
#: permanent failure. Chosen to ride out a brief external lock (antivirus,
#: backup tool) without stalling the writer thread for long.
_MAX_WRITE_RETRIES = 3
_RETRY_BACKOFF_BASE_SECONDS = 0.05


def _is_transient_sqlite_error(exc: BaseException) -> bool:
    """True for SQLite errors expected to clear up if retried shortly — the
    file was briefly locked by another process, or SQLITE_BUSY. False for
    programming errors (bad SQL, constraint violations) which retrying
    can't fix and would just delay recognizing as a permanent failure."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


@dataclass(frozen=True)
class DatabaseWriterHealth:
    """Snapshot of MonitorStore's background writer, for a future
    degraded-storage indicator in the UI."""
    queue_depth: int
    queue_capacity: int
    dropped_count: int
    failed_count: int
    writer_alive: bool
    last_error: str | None


def _db_path() -> Path:
    d = Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d / "monitor.db"


def _dt_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _parse_dt(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return None


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

        # Guards the counters below, which are written from the writer
        # thread and read from the GUI/caller thread via health().
        self._health_lock = threading.Lock()
        self._dropped_count = 0
        self._failed_count = 0
        self._last_error: str | None = None

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

    def update_message_status(self, local_id: str, packet_id: int | None, status) -> None:
        """Update a previously-saved outbound message's delivery status.

        Without this, only the initial SENDING row from save_message() ever
        existed in the DB — a later ACCEPTED_BY_RADIO/ACKNOWLEDGED/FAILED
        never got persisted, so restarting the app and reloading history
        showed every past outbound message stuck on "Sending…" again.
        """
        self._enqueue(("message_status", (local_id, packet_id, status)))

    def _enqueue(self, item: Any) -> None:
        try:
            self._write_q.put_nowait(item)
        except queue.Full:
            with self._health_lock:
                self._dropped_count += 1
                total_dropped = self._dropped_count
            log.warning(
                "MonitorStore write queue full (capacity %d), dropping item "
                "(%d dropped since open)",
                self._write_q.maxsize, total_dropped,
            )

    def health(self) -> DatabaseWriterHealth:
        """Point-in-time snapshot for a degraded-storage indicator."""
        with self._health_lock:
            dropped, failed, last_error = self._dropped_count, self._failed_count, self._last_error
        return DatabaseWriterHealth(
            queue_depth=self._write_q.qsize(),
            queue_capacity=self._write_q.maxsize,
            dropped_count=dropped,
            failed_count=failed,
            writer_alive=self._writer.is_alive(),
            last_error=last_error,
        )

    def _record_failed_item(self, kind: str, exc: BaseException) -> None:
        with self._health_lock:
            self._failed_count += 1
            self._last_error = f"{kind}: {exc}"
        log.error("MonitorStore permanently failed to write a %r item: %s", kind, exc)

    def _record_error(self, message: str) -> None:
        with self._health_lock:
            self._last_error = message

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

    def read_packets_as_objects(self, session_id: str, limit: int = 200_000) -> list[NetworkPacket]:
        """Like read_packets(), reconstructed as NetworkPacket objects.

        Used for exporting a session's full packet history — beyond what
        PacketIngestor's bounded in-memory ring buffer retains. `text` and
        `raw_metadata_json` are always None here: message text isn't stored
        in the packets table at all (it lives in `messages`, keyed by
        chat-relevant fields the packets table doesn't carry), so callers
        that need it must merge it back in themselves from whatever
        still-in-memory packets happen to overlap.
        """
        rows = self.read_packets(session_id, limit=limit)
        packets = []
        for r in rows:
            via_mqtt = r.get("via_mqtt")
            pki_encrypted = r.get("pki_encrypted")
            want_ack = r.get("want_ack")
            packets.append(NetworkPacket(
                session_id=r["session_id"],
                observed_at=_parse_dt(r.get("observed_at")) or datetime.now(timezone.utc),
                rx_time=_parse_dt(r.get("rx_time")),
                sender_num=r.get("sender_num"),
                sender_id=r.get("sender_id"),
                destination_num=r.get("destination_num"),
                packet_id=r.get("packet_id"),
                channel_index=r.get("channel_index"),
                portnum=r.get("portnum"),
                portnum_name=r.get("portnum_name") or "",
                text=None,
                payload_size=r.get("payload_size"),
                rx_snr=r.get("rx_snr"),
                rx_rssi=r.get("rx_rssi"),
                hop_start=r.get("hop_start"),
                hop_limit=r.get("hop_limit"),
                hops_used=r.get("hops_used"),
                via_mqtt=bool(via_mqtt) if via_mqtt is not None else None,
                transport_mechanism=r.get("transport_mechanism"),
                pki_encrypted=bool(pki_encrypted) if pki_encrypted is not None else None,
                want_ack=bool(want_ack) if want_ack is not None else None,
                priority=r.get("priority"),
                raw_metadata_json=None,
            ))
        return packets

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

        conn = None
        try:
            conn = self._read_conn()
            return self._prune_on(conn, retain_days)
        except Exception as exc:
            log.error("MonitorStore.prune failed: %s", exc)
            return {}
        finally:
            if conn is not None:
                conn.close()

    def prune_async(self, retain_days: int = DEFAULT_RETAIN_DAYS) -> None:
        """Queue a prune onto the existing writer thread.

        Deleting rows is blocking work; calling `prune()` directly from the GUI
        thread freezes the UI for as long as the DELETEs take. Routing it
        through the writer thread keeps the UI responsive and also keeps every
        write to this database serialized on one connection.
        """
        if retain_days > 0:
            self._enqueue(("prune", retain_days))

    @staticmethod
    def _prune_on(conn: sqlite3.Connection, retain_days: int) -> dict[str, int]:
        """Run the deletes on `conn` and report what was actually committed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()

        # Fixed statements rather than an interpolated table name: nothing here
        # is caller-controlled, and spelling them out removes any question of
        # SQL injection for both readers and scanners.
        statements = (
            ("packets", "DELETE FROM packets WHERE observed_at < ?"),
            ("positions", "DELETE FROM positions WHERE observed_at < ?"),
            ("telemetry", "DELETE FROM telemetry WHERE observed_at < ?"),
        )

        counts: dict[str, int] = {}
        with conn:  # commits on success, rolls back on any exception
            for table, sql in statements:
                cur = conn.execute(sql, (cutoff,))
                if cur.rowcount > 0:
                    counts[table] = cur.rowcount

        # Only published after the transaction above commits. Assigning inside
        # the block meant a failure on a later DELETE rolled back the earlier
        # ones while still reporting them as pruned.
        if counts:
            log.info("Pruned %d row(s) older than %d days: %s",
                     sum(counts.values()), retain_days, counts)
        return counts

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
            self._record_error(f"writer thread crashed: {exc}")

    def _write_one(self, conn: sqlite3.Connection, kind: str, obj: Any) -> None:
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
        elif kind == "message_status":
            self._write_message_status(conn, obj)

    def _write_item_with_retry(self, conn: sqlite3.Connection, kind: str, obj: Any) -> bool:
        """Write one queued item inside its own SAVEPOINT so a failure here
        rolls back only this item, not the rest of the batch's transaction.

        A transient error (lock contention, SQLITE_BUSY) is retried with
        bounded backoff; anything else — or exhausting the retries — fails
        just this item. Returns True on success.
        """
        attempt = 0
        delay = _RETRY_BACKOFF_BASE_SECONDS
        while True:
            attempt += 1
            try:
                conn.execute("SAVEPOINT item_write")
                self._write_one(conn, kind, obj)
                conn.execute("RELEASE SAVEPOINT item_write")
                return True
            except Exception as exc:
                conn.execute("ROLLBACK TO SAVEPOINT item_write")
                conn.execute("RELEASE SAVEPOINT item_write")
                if _is_transient_sqlite_error(exc) and attempt <= _MAX_WRITE_RETRIES:
                    log.warning(
                        "MonitorStore write retry %d/%d for %r item: %s",
                        attempt, _MAX_WRITE_RETRIES, kind, exc,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                self._record_failed_item(kind, exc)
                return False

    def _flush(self, conn: sqlite3.Connection, batch: list) -> None:
        # Prunes carry their own transaction, so run them outside the batch's
        # transaction rather than nesting transactions on the same connection.
        prunes = [obj for kind, obj in batch if kind == "prune"]
        batch = [item for item in batch if item[0] != "prune"]

        if batch:
            # Items _write_item_with_retry reports success for, in case the
            # transaction as a whole later fails to commit (see below).
            completed: list[tuple[str, Any]] = []
            try:
                with conn:
                    for kind, obj in batch:
                        if self._write_item_with_retry(conn, kind, obj):
                            completed.append((kind, obj))
            except Exception as exc:
                # Reached only if something outside per-item handling broke
                # — e.g. COMMIT itself failed (disk full). `with conn:`
                # rolls back the ENTIRE transaction in that case, so every
                # item that reported success above was actually lost too;
                # re-record those as failed. Items _write_item_with_retry
                # already recorded as failed are not touched again here.
                log.error("MonitorStore flush error: %s", exc, exc_info=True)
                self._record_error(str(exc))
                for kind, obj in completed:
                    self._record_failed_item(kind, exc)

        for retain_days in prunes:
            try:
                self._prune_on(conn, retain_days)
            except Exception as exc:
                log.error("MonitorStore prune failed: %s", exc)

    def _write_packet(self, conn: sqlite3.Connection, pkt: NetworkPacket) -> None:
        # Plain INSERT, not "OR IGNORE": the packets table's only key is its
        # auto-incrementing rowid (see database/schema.py — no UNIQUE
        # constraint on any other column), which is always new, so "OR
        # IGNORE" could never actually trigger a conflict here — it looked
        # like a DB-level dedup safety net but was a no-op. Real dedup
        # already happens upstream, in-memory, in
        # PacketIngestor._is_duplicate() before a packet ever reaches this
        # method; adding a genuine UNIQUE constraint here would risk
        # silently rejecting legitimate packets if the composite key isn't
        # chosen exactly right, which is worse than the status quo.
        conn.execute(
            """INSERT INTO packets
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
             first_seen, last_heard, packet_count, text_count,
             last_snr, last_rssi, last_hops_used, last_hop_start, last_via_mqtt,
             rf_count, via_mqtt_count, position_count, telemetry_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(node_num) DO UPDATE SET
                node_id=excluded.node_id,
                long_name=COALESCE(excluded.long_name, long_name),
                short_name=COALESCE(excluded.short_name, short_name),
                role=COALESCE(excluded.role, role),
                hw_model=COALESCE(excluded.hw_model, hw_model),
                -- MAX(), not a bare overwrite: the in-memory NodeSnapshot these
                -- values come from is already monotonic within one running
                -- app instance, but this is the actual persistence boundary —
                -- it shouldn't silently rely on every caller (present and
                -- future) upholding that invariant. SQLite's multi-arg
                -- max(a,b) is the scalar (not aggregate) form, but unlike the
                -- aggregate it returns NULL if EITHER argument is NULL — and
                -- last_heard has no NOT NULL/DEFAULT (a node can be seeded
                -- before ever being "heard"), so it's wrapped in COALESCE to
                -- fall back to whichever side is non-NULL instead of losing
                -- data. packet_count/text_count can't be NULL (NodeSnapshot's
                -- fields default to 0, never None), so a bare MAX is fine.
                last_heard=COALESCE(MAX(excluded.last_heard, last_heard), excluded.last_heard, last_heard),
                packet_count=MAX(excluded.packet_count, packet_count),
                text_count=MAX(excluded.text_count, text_count),
                -- The last_* fields describe the single most recent packet,
                -- not a running total — MAX() would be wrong here (e.g. a
                -- fresher 0-hop packet must not lose to a staler 3-hop one
                -- just because 3 > 0). Take them together only when this
                -- write's last_heard is at least as new as what's stored,
                -- so they always describe the same observation last_heard
                -- itself was just updated to.
                last_snr = CASE WHEN excluded.last_heard IS NOT NULL
                    AND (last_heard IS NULL OR excluded.last_heard >= last_heard)
                    THEN excluded.last_snr ELSE last_snr END,
                last_rssi = CASE WHEN excluded.last_heard IS NOT NULL
                    AND (last_heard IS NULL OR excluded.last_heard >= last_heard)
                    THEN excluded.last_rssi ELSE last_rssi END,
                last_hops_used = CASE WHEN excluded.last_heard IS NOT NULL
                    AND (last_heard IS NULL OR excluded.last_heard >= last_heard)
                    THEN excluded.last_hops_used ELSE last_hops_used END,
                last_hop_start = CASE WHEN excluded.last_heard IS NOT NULL
                    AND (last_heard IS NULL OR excluded.last_heard >= last_heard)
                    THEN excluded.last_hop_start ELSE last_hop_start END,
                last_via_mqtt = CASE WHEN excluded.last_heard IS NOT NULL
                    AND (last_heard IS NULL OR excluded.last_heard >= last_heard)
                    THEN excluded.last_via_mqtt ELSE last_via_mqtt END,
                rf_count=MAX(excluded.rf_count, rf_count),
                via_mqtt_count=MAX(excluded.via_mqtt_count, via_mqtt_count),
                position_count=MAX(excluded.position_count, position_count),
                telemetry_count=MAX(excluded.telemetry_count, telemetry_count)""",
            (
                node.node_num, node.node_id, node.long_name, node.short_name,
                node.role, node.hw_model,
                _dt_str(node.first_seen), _dt_str(node.last_heard),
                node.packet_count, node.text_count,
                node.last_snr, node.last_rssi, node.last_hops_used, node.last_hop_start,
                int(node.last_via_mqtt) if node.last_via_mqtt is not None else None,
                node.rf_count, node.via_mqtt_count, node.position_count, node.telemetry_count,
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
                msg.session_id, msg.local_id, msg.packet_id, msg.channel_index, msg.destination_num,
                msg.direction.value, msg.sender_num, msg.sender_id, msg.sender_name, msg.text,
                _dt_str(msg.timestamp), len(msg.text.encode("utf-8")), msg.status.value,
            ),
        )

    def _write_message_status(self, conn: sqlite3.Connection, update: tuple) -> None:
        local_id, packet_id, status = update
        # COALESCE: a status update doesn't always carry a packet_id (e.g. a
        # FAILED event on a send that never got one) — don't clobber an
        # already-known packet_id with NULL in that case.
        conn.execute(
            "UPDATE messages SET status=?, packet_id=COALESCE(?, packet_id) WHERE local_id=?",
            (status.value, packet_id, local_id),
        )

    def _read_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
