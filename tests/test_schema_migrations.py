"""Tests for the schema-versioned migration mechanism in database/schema.py.

Covers: migrating from historical (pre-versioning) schema shapes without
losing existing rows, idempotency (re-applying is a no-op once current),
the pre-migration backup, and that a migration/integrity failure raises a
clear MigrationError rather than silently corrupting or discarding data.
"""
from __future__ import annotations

import sqlite3

import pytest

from meshchat.database.schema import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    apply_schema,
)

# A pre-migration ("v0") schema shape: messages has no text/destination_num
# columns, nodes has none of the persisted signal/hop/transport-stat
# columns. Approximates the schema shape before those columns were added,
# to exercise migrating a real historical database.
_LEGACY_V0_SCHEMA = """
CREATE TABLE sessions (
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

-- packets/positions/telemetry match current SCHEMA_SQL exactly: these
-- tables' columns have never changed, only messages/nodes have (the two
-- tables the real _migrate()/migration functions touch) — a mismatch here
-- would fail on the CREATE INDEX statements SCHEMA_SQL runs against them,
-- for reasons unrelated to what this module actually tests.
CREATE TABLE packets (
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

CREATE TABLE nodes (
    node_num        INTEGER PRIMARY KEY,
    node_id         TEXT,
    long_name       TEXT,
    short_name      TEXT,
    role            TEXT,
    hw_model        TEXT,
    first_seen      TEXT,
    last_heard      TEXT,
    packet_count    INTEGER DEFAULT 0,
    text_count      INTEGER DEFAULT 0
);

CREATE TABLE positions (
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

CREATE TABLE telemetry (
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

CREATE TABLE messages (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    local_id        TEXT UNIQUE,
    packet_id       INTEGER,
    channel_index   INTEGER,
    direction       TEXT,
    sender_num      INTEGER,
    sender_id       TEXT,
    sender_name     TEXT,
    observed_at     TEXT NOT NULL,
    byte_count      INTEGER,
    status          TEXT
);
"""

# A "v1" shape: messages already has text/destination_num (an earlier
# migration already applied), but nodes still lacks the stat columns.
_LEGACY_V1_SCHEMA = _LEGACY_V0_SCHEMA.replace(
    "status          TEXT\n);",
    "status          TEXT,\n    text            TEXT,\n    destination_num INTEGER\n);",
)


def _seed_legacy_db(db_path, schema_sql: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema_sql)
    conn.execute(
        "INSERT INTO messages (session_id, local_id, sender_name, observed_at, status) "
        "VALUES ('s1', 'm1', 'Alice', '2024-01-01T00:00:00+00:00', 'received')"
    )
    conn.execute(
        "INSERT INTO nodes (node_num, long_name, packet_count) VALUES (42, 'Test Node', 7)"
    )
    conn.commit()
    conn.close()


class TestMigrationFromHistoricalSchemas:
    def test_migrates_v0_schema_and_preserves_existing_rows(self, tmp_path):
        db_path = tmp_path / "legacy_v0.db"
        _seed_legacy_db(db_path, _LEGACY_V0_SCHEMA)

        conn = sqlite3.connect(str(db_path))
        apply_schema(conn, db_path)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION

        msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert {"text", "destination_num"} <= msg_cols
        node_cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        assert {"last_snr", "rf_count", "position_count", "telemetry_count"} <= node_cols

        # Pre-existing rows survived, untouched.
        msg_row = conn.execute("SELECT sender_name, status FROM messages WHERE local_id='m1'").fetchone()
        assert msg_row == ("Alice", "received")
        node_row = conn.execute("SELECT long_name, packet_count FROM nodes WHERE node_num=42").fetchone()
        assert node_row == ("Test Node", 7)
        conn.close()

    def test_migrates_v1_schema_adding_only_node_columns(self, tmp_path):
        db_path = tmp_path / "legacy_v1.db"
        _seed_legacy_db(db_path, _LEGACY_V1_SCHEMA)

        conn = sqlite3.connect(str(db_path))
        apply_schema(conn, db_path)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        node_cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        assert "last_snr" in node_cols
        row = conn.execute("SELECT long_name FROM nodes WHERE node_num=42").fetchone()
        assert row == ("Test Node",)
        conn.close()

    def test_creates_pre_migration_backup_for_existing_database(self, tmp_path):
        db_path = tmp_path / "legacy_v0.db"
        _seed_legacy_db(db_path, _LEGACY_V0_SCHEMA)

        conn = sqlite3.connect(str(db_path))
        apply_schema(conn, db_path)
        conn.close()

        backups = list((tmp_path / "backups").glob("legacy_v0.schema-v0.*.db"))
        assert len(backups) == 1

    def test_idempotent_reapply_is_a_no_op(self, tmp_path):
        db_path = tmp_path / "legacy_v0.db"
        _seed_legacy_db(db_path, _LEGACY_V0_SCHEMA)

        conn = sqlite3.connect(str(db_path))
        apply_schema(conn, db_path)
        conn.close()

        backups_after_first = list((tmp_path / "backups").glob("*"))

        conn = sqlite3.connect(str(db_path))
        apply_schema(conn, db_path)  # already at CURRENT_SCHEMA_VERSION — must be a no-op
        conn.close()

        backups_after_second = list((tmp_path / "backups").glob("*"))
        assert backups_after_second == backups_after_first


class TestNewDatabase:
    def test_fresh_database_reaches_current_version_without_a_backup(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        conn = sqlite3.connect(str(db_path))
        apply_schema(conn, db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        conn.close()
        assert not (tmp_path / "backups").exists()

    def test_db_path_is_optional(self, tmp_path):
        # In-memory / throwaway connections have nothing to back up.
        conn = sqlite3.connect(":memory:")
        apply_schema(conn)  # must not raise despite no db_path
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        conn.close()


class TestMigrationFailureIsSafe:
    def test_migration_error_does_not_lose_existing_data(self, tmp_path, monkeypatch):
        import meshchat.database.schema as schema_module

        db_path = tmp_path / "legacy_v0.db"
        _seed_legacy_db(db_path, _LEGACY_V0_SCHEMA)

        def boom(conn):
            raise RuntimeError("simulated migration failure")

        # _MIGRATIONS captured the real function by reference when the module
        # loaded, so patching the module attribute alone wouldn't affect the
        # already-bound entry in that list — patch the list entry itself.
        monkeypatch.setattr(
            schema_module, "_MIGRATIONS",
            [(1, boom), (2, schema_module._migrate_v2_node_stat_columns)],
        )

        conn = sqlite3.connect(str(db_path))
        with pytest.raises(MigrationError, match="schema version 1 failed"):
            apply_schema(conn, db_path)
        conn.close()

        # Reopen fresh and confirm the pre-existing row is untouched and no
        # partial migration state (e.g. a bumped user_version) leaked out.
        conn = sqlite3.connect(str(db_path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        row = conn.execute("SELECT sender_name FROM messages WHERE local_id='m1'").fetchone()
        assert row == ("Alice",)
        conn.close()

    def test_integrity_check_failure_raises_clear_error(self, tmp_path):
        class _FakeIntegrityFailConn(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.strip() == "PRAGMA integrity_check":
                    class _FakeCursor:
                        def fetchone(self_inner):
                            return ("corruption detected",)
                    return _FakeCursor()
                return super().execute(sql, *args, **kwargs)

        db_path = tmp_path / "legacy_v0.db"
        _seed_legacy_db(db_path, _LEGACY_V0_SCHEMA)

        conn = sqlite3.connect(str(db_path), factory=_FakeIntegrityFailConn)
        with pytest.raises(MigrationError, match="integrity check failed"):
            apply_schema(conn, db_path)
        conn.close()
