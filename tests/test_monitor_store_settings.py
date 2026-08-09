"""Tests for MonitorStore get_setting / set_setting (app_settings table)."""
from __future__ import annotations

import pytest

from meshchat.services.monitor_store import MonitorStore


@pytest.fixture
def store(tmp_path):
    s = MonitorStore(db_path=tmp_path / "test.db")
    yield s
    s.shutdown()


def _flush(store: MonitorStore) -> None:
    """Block until all pending writes are committed.

    Uses backup() which enqueues a sentinel that only fires after every
    prior item in the queue has been written — without tearing down the
    writer thread so subsequent writes in the same test still work.
    """
    store.backup()


class TestGetSettingMissing:
    def test_returns_none_for_unknown_key(self, store):
        assert store.get_setting("no.such.key") is None


class TestSetThenGet:
    def test_round_trips_a_string_value(self, store):
        store.set_setting("connection.transport", "tcp")
        _flush(store)
        assert store.get_setting("connection.transport") == "tcp"

    def test_overwrites_existing_value(self, store):
        store.set_setting("connection.tcp_host", "192.168.1.1")
        _flush(store)
        store.set_setting("connection.tcp_host", "10.0.0.5")
        _flush(store)
        assert store.get_setting("connection.tcp_host") == "10.0.0.5"

    def test_stores_empty_string(self, store):
        store.set_setting("connection.ble_address", "")
        _flush(store)
        assert store.get_setting("connection.ble_address") == ""

    def test_stores_numeric_string(self, store):
        store.set_setting("connection.tcp_port", "4403")
        _flush(store)
        assert store.get_setting("connection.tcp_port") == "4403"


class TestMultipleKeys:
    def test_independent_keys_do_not_collide(self, store):
        store.set_setting("connection.transport", "serial")
        store.set_setting("connection.serial_port", "COM3")
        _flush(store)
        assert store.get_setting("connection.transport") == "serial"
        assert store.get_setting("connection.serial_port") == "COM3"


class TestPersistence:
    def test_survives_store_restart(self, tmp_path):
        db = tmp_path / "persist.db"
        s1 = MonitorStore(db_path=db)
        s1.set_setting("connection.transport", "tcp")
        _flush(s1)
        s1.shutdown()

        s2 = MonitorStore(db_path=db)
        try:
            assert s2.get_setting("connection.transport") == "tcp"
        finally:
            s2.shutdown()
