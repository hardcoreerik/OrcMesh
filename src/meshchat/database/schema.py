"""MeshChat – SQLite database schema."""
from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    local_node_num  INTEGER,
    local_node_id   TEXT,
    transport       TEXT,
    connection_target TEXT,
    meshtastic_version TEXT,
    firmware_version TEXT
);

CREATE TABLE IF NOT EXISTS packets (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    rx_time         TEXT,
    sender_num      INTEGER,
    sender_id       TEXT,
    destination_num INTEGER,
    packet_id       INTEGER,
    channel_index   INTEGER,
    portnum         INTEGER,
    portnum_name    TEXT,
    payload_size    INTEGER,
    rx_snr          REAL,
    rx_rssi         INTEGER,
    hop_start       INTEGER,
    hop_limit       INTEGER,
    hops_used       INTEGER,
    via_mqtt        INTEGER,
    transport_mechanism TEXT,
    pki_encrypted   INTEGER,
    want_ack        INTEGER,
    priority        TEXT
);

CREATE INDEX IF NOT EXISTS idx_packets_session_time
    ON packets(session_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_packets_sender_time
    ON packets(sender_num, observed_at);

CREATE INDEX IF NOT EXISTS idx_packets_portnum_time
    ON packets(portnum, observed_at);

CREATE INDEX IF NOT EXISTS idx_packets_hops_time
    ON packets(hops_used, observed_at);

CREATE TABLE IF NOT EXISTS nodes (
    node_num        INTEGER PRIMARY KEY,
    node_id         TEXT,
    long_name       TEXT,
    short_name      TEXT,
    role            TEXT,
    hw_model        TEXT,
    first_seen      TEXT,
    last_heard      TEXT,
    packet_count    INTEGER DEFAULT 0,
    text_count      INTEGER DEFAULT 0,
    -- Signal/hop/transport state of the most recently heard packet, plus
    -- session-lifetime RF/MQTT/position/telemetry counters — previously
    -- tracked only on the in-memory NodeSnapshot, so a node the app had
    -- tracked for weeks started every restart with these reset to
    -- zero/unknown until it was heard again live. See MonitorStore._write_node.
    last_snr        REAL,
    last_rssi       INTEGER,
    last_hops_used  INTEGER,
    last_hop_start  INTEGER,
    last_via_mqtt   INTEGER,
    rf_count        INTEGER DEFAULT 0,
    via_mqtt_count  INTEGER DEFAULT 0,
    position_count  INTEGER DEFAULT 0,
    telemetry_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_num        INTEGER NOT NULL,
    observed_at     TEXT NOT NULL,
    latitude        REAL,
    longitude       REAL,
    altitude_m      REAL,
    speed_ms        REAL,
    heading_deg     REAL,
    position_time   TEXT,
    precision_bits  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_positions_node_time
    ON positions(node_num, observed_at);

CREATE TABLE IF NOT EXISTS telemetry (
    rowid               INTEGER PRIMARY KEY AUTOINCREMENT,
    node_num            INTEGER NOT NULL,
    observed_at         TEXT NOT NULL,
    battery_level       REAL,
    voltage             REAL,
    channel_utilization REAL,
    air_util_tx         REAL,
    temperature_c       REAL,
    relative_humidity   REAL,
    barometric_pressure_hpa REAL,
    gas_resistance_ohm  REAL
);

CREATE TABLE IF NOT EXISTS messages (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    local_id        TEXT UNIQUE,
    packet_id       INTEGER,
    channel_index   INTEGER,
    destination_num INTEGER,
    direction       TEXT,
    sender_num      INTEGER,
    sender_id       TEXT,
    sender_name     TEXT,
    text            TEXT,
    observed_at     TEXT NOT NULL,
    byte_count      INTEGER,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Indexes that reference columns added by migrations — must run after
# migration, not inside SCHEMA_SQL, or CREATE INDEX fails on pre-migration DBs.
_POST_MIGRATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_channel_time
    ON messages(channel_index, observed_at);

CREATE INDEX IF NOT EXISTS idx_messages_destination_time
    ON messages(destination_num, observed_at);

-- Neither composite index above covers a plain, unfiltered ORDER BY
-- observed_at (SQLite can only use a composite index for this via its
-- leftmost column, channel_index/destination_num, neither of which this
-- query filters on) — and MonitorStore.read_messages() does exactly that
-- to load chat history on every app startup.
CREATE INDEX IF NOT EXISTS idx_messages_time
    ON messages(observed_at);
"""


class MigrationError(RuntimeError):
    """Raised when a schema migration cannot be completed safely.

    Callers should surface `str(exc)` to the user as-is — it already
    explains what happened and, when a backup was taken, where to find it.
    No migration in this module deletes or overwrites existing rows, only
    adds columns, so raising here always leaves the database exactly as it
    was before this attempt (the failing migration's own ALTER TABLE is
    rolled back by its transaction).
    """


def _migrate_v1_message_columns(conn: sqlite3.Connection) -> None:
    """Add messages.text / messages.destination_num, absent from the
    original release before direct-message support and inline text
    storage. Idempotent: checked independently of user_version so a
    partially-migrated or hand-edited DB still converges correctly."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "text" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN text TEXT")
    if "destination_num" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN destination_num INTEGER")


def _migrate_v2_node_stat_columns(conn: sqlite3.Connection) -> None:
    """Add the nodes table's persisted signal/hop/transport-stat columns
    (previously tracked only on the in-memory NodeSnapshot — see
    MonitorStore._write_node). Idempotent for the same reason as v1."""
    node_cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    for col, decl in (
        ("last_snr", "REAL"),
        ("last_rssi", "INTEGER"),
        ("last_hops_used", "INTEGER"),
        ("last_hop_start", "INTEGER"),
        ("last_via_mqtt", "INTEGER"),
        ("rf_count", "INTEGER DEFAULT 0"),
        ("via_mqtt_count", "INTEGER DEFAULT 0"),
        ("position_count", "INTEGER DEFAULT 0"),
        ("telemetry_count", "INTEGER DEFAULT 0"),
    ):
        if col not in node_cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {decl}")


#: Sequential, idempotent migrations, each tagged with the schema version it
#: brings the database to. A brand-new database created from SCHEMA_SQL
#: already has every column these add, so PRAGMA user_version is set to
#: CURRENT_SCHEMA_VERSION immediately on first open without actually running
#: any of them (see _run_migrations). Add new migrations by appending here —
#: never renumber or remove an existing entry, or a database already
#: stamped with that version number will silently skip it.
_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migrate_v1_message_columns),
    (2, _migrate_v2_node_stat_columns),
]

CURRENT_SCHEMA_VERSION = _MIGRATIONS[-1][0]


def apply_schema(conn: sqlite3.Connection, db_path: Path | None = None) -> None:
    """Apply the full schema to an existing SQLite connection, running any
    migrations the database still needs to reach CURRENT_SCHEMA_VERSION.

    `db_path` enables the pre-migration backup (see _run_migrations) — pass
    it whenever `conn` is backed by a real file. Safe to omit for in-memory
    or throwaway connections, which have nothing worth backing up.
    """
    # Checked before executescript creates the schema: a brand-new database
    # has nothing worth backing up, and would otherwise get a pointless
    # empty-schema "backup" on every fresh install (its user_version also
    # starts at 0, indistinguishable from a genuinely old pre-migration DB,
    # so that check alone can't tell the two apart).
    is_new_database = db_path is None or not db_path.exists() or db_path.stat().st_size == 0

    conn.executescript(SCHEMA_SQL)
    _run_migrations(conn, None if is_new_database else db_path)
    conn.executescript(_POST_MIGRATE_INDEXES)
    conn.commit()


def _backup_before_migration(conn: sqlite3.Connection, db_path: Path, from_version: int) -> Path | None:
    """Copy the database file to a timestamped backup before a migration
    touches it. Returns the backup path, or None if there was nothing to
    back up (a brand-new/empty database) or the backup itself failed.

    A failed backup does not block the migration — see the caller — but is
    always logged, since it changes what recovery looks like if the
    migration itself then fails.
    """
    try:
        if not db_path.exists() or db_path.stat().st_size == 0:
            return None
        # WAL mode means committed data can still be sitting in the -wal
        # file rather than the main database file; checkpoint first so the
        # copy actually contains everything.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"{db_path.stem}.schema-v{from_version}.{stamp}{db_path.suffix}"
        shutil.copy2(db_path, backup_path)
        log.info("Backed up database (schema v%d) to %s before migration", from_version, backup_path)
        return backup_path
    except Exception as exc:
        log.error("Pre-migration backup failed (continuing without one): %s", exc)
        return None


def _run_migrations(conn: sqlite3.Connection, db_path: Path | None) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    pending = [(version, fn) for version, fn in _MIGRATIONS if version > current]
    if not pending:
        return

    backup_path: Path | None = None
    if db_path is not None:
        backup_path = _backup_before_migration(conn, db_path, current)
    backup_note = f" A pre-migration backup was saved to {backup_path}." if backup_path else ""

    for version, migrate_fn in pending:
        try:
            with conn:
                migrate_fn(conn)
                # PRAGMA user_version writes the database header page like
                # any other write in the current transaction, so it commits
                # or rolls back together with this migration's ALTER TABLEs
                # — never left pointing past a migration that didn't apply.
                conn.execute(f"PRAGMA user_version = {version}")
        except Exception as exc:
            raise MigrationError(
                f"Database migration to schema version {version} failed: {exc}. "
                f"Your existing data has not been modified.{backup_note}"
            ) from exc

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(
            f"Database integrity check failed after migration: {integrity}."
            f"{backup_note} If you have a backup, restore it and report this issue."
        )
