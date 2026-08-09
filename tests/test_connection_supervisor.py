"""Tests for ConnectionSupervisor: profile persistence, backoff scheduling,
and reconnect-after-loss behaviour.

All tests use a real QApplication (via the qt_app fixture) so that QTimer
and QObject signals/slots behave as they do in production.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

try:
    from PySide6.QtCore import QCoreApplication
    _QT_AVAILABLE = True
except ImportError:
    _QT_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _QT_AVAILABLE, reason="PySide6 not installed")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def qt_app():
    import sys
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    yield app


@pytest.fixture
def mock_controller():
    # Provide real Signal-like objects so ConnectionSupervisor can connect to them.
    from PySide6.QtCore import QObject, Signal

    class _FakeController(QObject):
        connected = Signal(object)
        disconnected = Signal(str)
        error_occurred = Signal(object)

    obj = _FakeController()
    obj.connect_tcp = MagicMock()
    obj.connect_ble = MagicMock()
    obj.connect_serial = MagicMock()
    obj.enter_reconnecting = MagicMock()
    return obj


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.get_setting.return_value = None
    return store


@pytest.fixture
def supervisor(qt_app, mock_controller, mock_store):
    from meshchat.services.connection_supervisor import ConnectionSupervisor
    return ConnectionSupervisor(mock_controller, mock_store)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_error(code_value: str, recoverable: bool = True):
    from meshchat.controllers.meshtastic_controller import ErrorCode
    err = MagicMock()
    err.code = ErrorCode(code_value)
    err.recoverable = recoverable
    return err


def _make_summary():
    s = MagicMock()
    s.long_name = "TestRadio"
    s.short_name = "TR"
    s.node_id = "!aabbccdd"
    s.node_num = 12345
    return s


# ── load_profile ──────────────────────────────────────────────────────────────

class TestLoadProfile:
    def test_returns_none_when_no_transport_key(self, mock_store):
        from meshchat.services.connection_supervisor import ConnectionSupervisor
        mock_store.get_setting.return_value = None
        assert ConnectionSupervisor.load_profile(mock_store) is None

    def test_returns_profile_with_tcp_transport(self, mock_store):
        from meshchat.services.connection_supervisor import ConnectionSupervisor

        def _get(key):
            return {
                "connection.transport": "tcp",
                "connection.tcp_host": "192.168.1.1",
                "connection.tcp_port": "4403",
                "connection.ble_address": "",
                "connection.serial_port": "",
            }.get(key)

        mock_store.get_setting.side_effect = _get
        profile = ConnectionSupervisor.load_profile(mock_store)
        assert profile is not None
        assert profile.transport == "tcp"
        assert profile.tcp_host == "192.168.1.1"
        assert profile.tcp_port == 4403

    def test_handles_invalid_port_gracefully(self, mock_store):
        from meshchat.services.connection_supervisor import ConnectionSupervisor

        def _get(key):
            return {"connection.transport": "tcp", "connection.tcp_port": "not-a-number"}.get(key)

        mock_store.get_setting.side_effect = _get
        profile = ConnectionSupervisor.load_profile(mock_store)
        assert profile is not None
        assert profile.tcp_port == 4403   # falls back to default


# ── set_profile / _save_profile ───────────────────────────────────────────────

class TestSetAndSaveProfile:
    def test_set_profile_is_stored(self, supervisor):
        from meshchat.models.connection_profile import ConnectionProfile
        p = ConnectionProfile(transport="tcp", tcp_host="10.0.0.1", tcp_port=4403)
        supervisor.set_profile(p)
        assert supervisor._profile is p

    def test_save_profile_writes_all_keys(self, supervisor, mock_store):
        from meshchat.models.connection_profile import ConnectionProfile
        p = ConnectionProfile(transport="tcp", tcp_host="10.0.0.1", tcp_port=4403)
        supervisor.set_profile(p)
        supervisor._save_profile()

        written = {call.args[0]: call.args[1] for call in mock_store.set_setting.call_args_list}
        assert written["connection.transport"] == "tcp"
        assert written["connection.tcp_host"] == "10.0.0.1"
        assert written["connection.tcp_port"] == "4403"

    def test_save_profile_noop_when_none(self, supervisor, mock_store):
        supervisor._profile = None
        supervisor._save_profile()
        mock_store.set_setting.assert_not_called()


# ── cancel ────────────────────────────────────────────────────────────────────

class TestCancel:
    def test_cancel_clears_active_and_was_connected(self, supervisor):
        supervisor._active = True
        supervisor._was_connected = True
        supervisor.cancel()
        assert not supervisor._active
        assert not supervisor._was_connected

    def test_cancel_stops_timer(self, supervisor):
        supervisor._timer.start(10_000)
        assert supervisor._timer.isActive()
        supervisor.cancel()
        assert not supervisor._timer.isActive()


# ── _on_connected ─────────────────────────────────────────────────────────────

class TestOnConnected:
    def test_sets_was_connected_and_resets_attempt(self, supervisor):
        supervisor._attempt = 3
        supervisor._on_connected(_make_summary())
        assert supervisor._was_connected is True
        assert supervisor._attempt == 0
        assert supervisor._active is False

    def test_saves_profile_on_connect(self, supervisor, mock_store):
        from meshchat.models.connection_profile import ConnectionProfile
        supervisor.set_profile(ConnectionProfile(transport="serial", serial_port="COM5"))
        supervisor._on_connected(_make_summary())
        mock_store.set_setting.assert_called()


# ── _on_error / auto-reconnect ────────────────────────────────────────────────

class TestOnError:
    def test_connection_lost_after_success_starts_retry(self, supervisor, mock_controller):
        from meshchat.models.connection_profile import ConnectionProfile
        supervisor.set_profile(ConnectionProfile(transport="tcp", tcp_host="h", tcp_port=4403))
        supervisor._was_connected = True

        supervisor._on_error(_make_error("connection_lost"))

        assert supervisor._active is True
        assert supervisor._attempt == 0
        mock_controller.enter_reconnecting.assert_called_once()

    def test_connection_lost_without_prior_success_does_nothing(self, supervisor, mock_controller):
        from meshchat.models.connection_profile import ConnectionProfile
        supervisor.set_profile(ConnectionProfile(transport="tcp", tcp_host="h", tcp_port=4403))
        supervisor._was_connected = False

        supervisor._on_error(_make_error("connection_lost"))

        assert supervisor._active is False
        mock_controller.enter_reconnecting.assert_not_called()

    def test_non_connection_lost_error_does_nothing_when_inactive(self, supervisor, mock_controller):
        from meshchat.models.connection_profile import ConnectionProfile
        supervisor.set_profile(ConnectionProfile(transport="tcp", tcp_host="h", tcp_port=4403))
        supervisor._was_connected = True
        supervisor._on_error(_make_error("ble_not_available"))
        assert supervisor._active is False

    def test_active_retry_failure_increments_attempt(self, supervisor, mock_controller):
        from meshchat.models.connection_profile import ConnectionProfile
        supervisor.set_profile(ConnectionProfile(transport="tcp", tcp_host="h", tcp_port=4403))
        supervisor._was_connected = True
        supervisor._active = True
        supervisor._attempt = 1

        supervisor._on_error(_make_error("connection_lost"))

        assert supervisor._attempt == 2
        assert mock_controller.enter_reconnecting.call_count == 1


# ── _attempt_reconnect ────────────────────────────────────────────────────────

class TestAttemptReconnect:
    def test_calls_connect_tcp(self, supervisor, mock_controller):
        from meshchat.models.connection_profile import ConnectionProfile
        supervisor.set_profile(ConnectionProfile(transport="tcp", tcp_host="myhost", tcp_port=4403))
        supervisor._active = True
        supervisor._attempt_reconnect()
        mock_controller.connect_tcp.assert_called_once_with("myhost", 4403)

    def test_calls_connect_ble(self, supervisor, mock_controller):
        from meshchat.models.connection_profile import ConnectionProfile
        supervisor.set_profile(ConnectionProfile(transport="ble", ble_address="AA:BB:CC:DD"))
        supervisor._active = True
        supervisor._attempt_reconnect()
        mock_controller.connect_ble.assert_called_once_with("AA:BB:CC:DD")

    def test_calls_connect_serial(self, supervisor, mock_controller):
        from meshchat.models.connection_profile import ConnectionProfile
        supervisor.set_profile(ConnectionProfile(transport="serial", serial_port="COM3"))
        supervisor._active = True
        supervisor._attempt_reconnect()
        mock_controller.connect_serial.assert_called_once_with("COM3")

    def test_does_nothing_when_inactive(self, supervisor, mock_controller):
        from meshchat.models.connection_profile import ConnectionProfile
        supervisor.set_profile(ConnectionProfile(transport="tcp", tcp_host="h", tcp_port=4403))
        supervisor._active = False
        supervisor._attempt_reconnect()
        mock_controller.connect_tcp.assert_not_called()
