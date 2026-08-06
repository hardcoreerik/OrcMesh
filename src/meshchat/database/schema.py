"""MeshChat – SQLite database schema."""
from __future__ import annotations

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

# Indexes that reference columns added by _migrate() — must run after
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
-- ("SELECT * FROM messages ORDER BY observed_at ASC LIMIT ?") to load
-- chat history on every app startup.
CREATE INDEX IF NOT EXISTS idx_messages_time
    ON messages(observed_at);
"""


def apply_schema(conn) -> None:
    """Apply the full schema to an existing SQLite connection."""
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    conn.executescript(_POST_MIGRATE_INDEXES)
    conn.commit()


def _migrate(conn) -> None:
    """Add columns to tables created by older versions of this schema."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "text" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN text TEXT")
    if "destination_num" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN destination_num INTEGER")

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
