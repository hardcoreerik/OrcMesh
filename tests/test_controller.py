"""Tests for MeshtasticController state machine using FakeMeshtasticInterface.

These tests do NOT require real radio hardware. They patch the BLE/TCP
interface constructors with a side_effect factory so each fake is
instantiated on the worker thread AFTER PubSub subscriptions are set up.

NOTE: The controller uses QThread; Qt signals require a QCoreApplication.
"""
from __future__ import annotations

import sys
import unittest.mock as mock
from contextlib import contextmanager
from typing import List

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.controllers.meshtastic_controller import (
    ConnectionState,
    MeshtasticController,
)
from tests.fakes.fake_meshtastic_interface import FakeMeshtasticInterface


def _pump_events(ms: int = 500) -> None:
    """Process Qt events for up to `ms` milliseconds."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _worker_state(ctrl: MeshtasticController) -> ConnectionState:
    """Read current state from the worker (controller has no public .state)."""
    return ctrl._worker._state


@contextmanager
def _patch_ble(fakes_out: List[FakeMeshtasticInterface], *, raises=None):
    """
    Patch BLEInterface so a new FakeMeshtasticInterface is created on each call.
    The fake fires meshtastic.connection.established ~50ms after construction,
    which happens AFTER _subscribe() runs on the worker thread.
    Appends each created fake to fakes_out.
    """
    def factory(address=None, timeout=None, **kwargs):
        if raises:
            raise raises
        f = FakeMeshtasticInterface()
        fakes_out.append(f)
        return f

    with mock.patch("meshtastic.ble_interface.BLEInterface", side_effect=factory):
        yield


@contextmanager
def _patch_tcp(fakes_out: List[FakeMeshtasticInterface], *, raises=None):
    def factory(hostname=None, portNumber=None, timeout=None, **kwargs):
        if raises:
            raise raises
        f = FakeMeshtasticInterface()
        fakes_out.append(f)
        return f

    with mock.patch("meshtastic.tcp_interface.TCPInterface", side_effect=factory):
        yield


# ── State machine: initial state ──────────────────────────────────────────────

class TestInitialState:
    def test_starts_disconnected(self):
        ctrl = MeshtasticController()
        assert _worker_state(ctrl) == ConnectionState.DISCONNECTED
        ctrl.shutdown()

    def test_no_interface_at_start(self):
        ctrl = MeshtasticController()
        assert ctrl._worker._interface is None
        ctrl.shutdown()


# ── BLE connect flow ──────────────────────────────────────────────────────────

class TestBleConnect:
    def test_connect_ble_transitions_to_connected(self):
        fakes = []
        ctrl = MeshtasticController()
        states = []
        ctrl.connection_state_changed.connect(lambda s, d: states.append(s))

        with _patch_ble(fakes):
            ctrl.connect_ble("AA:BB:CC:DD:EE:FF")
            _pump_events(1000)  # enough for worker + 50ms timer + signal delivery

        assert ConnectionState.CONNECTED in states, f"States seen: {states}"
        ctrl.shutdown()

    def test_connect_ble_emits_connected_signal(self):
        fakes = []
        ctrl = MeshtasticController()
        connected_calls = []
        ctrl.connected.connect(connected_calls.append)

        with _patch_ble(fakes):
            ctrl.connect_ble("AA:BB:CC:DD:EE:FF")
            _pump_events(1000)

        assert len(connected_calls) >= 1, "connected signal not emitted"
        ctrl.shutdown()

    def test_connect_ble_failure_transitions_to_error_or_disconnected(self):
        fakes = []
        ctrl = MeshtasticController()
        states = []
        ctrl.connection_state_changed.connect(lambda s, d: states.append(s))

        with _patch_ble(fakes, raises=RuntimeError("No radio")):
            ctrl.connect_ble("AA:BB:CC:DD:EE:FF")
            _pump_events(500)

        assert any(s in (ConnectionState.ERROR, ConnectionState.DISCONNECTED) for s in states), (
            f"Expected ERROR or DISCONNECTED in: {states}"
        )
        ctrl.shutdown()

    def test_disconnect_after_connect(self):
        fakes = []
        ctrl = MeshtasticController()

        with _patch_ble(fakes):
            ctrl.connect_ble("AA:BB:CC:DD:EE:FF")
            _pump_events(1000)
            ctrl.disconnect()
            _pump_events(400)

        state = _worker_state(ctrl)
        assert state in (ConnectionState.DISCONNECTED, ConnectionState.DISCONNECTING), (
            f"Expected DISCONNECTED/DISCONNECTING, got {state}"
        )
        ctrl.shutdown()


# ── TCP connect flow ───────────────────────────────────────────────────────────

class TestTcpConnect:
    def test_connect_tcp_transitions_to_connected(self):
        fakes = []
        ctrl = MeshtasticController()
        states = []
        ctrl.connection_state_changed.connect(lambda s, d: states.append(s))

        with _patch_tcp(fakes):
            ctrl.connect_tcp("192.168.1.100", 4403)
            _pump_events(1000)

        assert ConnectionState.CONNECTED in states, f"States: {states}"
        ctrl.shutdown()


# ── Inbound packets ────────────────────────────────────────────────────────────

class TestInboundPackets:
    def test_inbound_text_emits_message_received(self):
        fakes = []
        ctrl = MeshtasticController()
        messages = []
        ctrl.message_received.connect(messages.append)

        with _patch_ble(fakes):
            ctrl.connect_ble("AA:BB:CC:DD:EE:FF")
            _pump_events(1000)
            assert fakes, "No fake interface was created"
            fakes[0].pump_text("Hello test")
            _pump_events(500)

        assert any("Hello test" in getattr(m, "text", "") for m in messages), (
            f"message_received not emitted; messages={messages}"
        )
        ctrl.shutdown()

    def test_inbound_raw_packet_emits_raw_packet_signal(self):
        fakes = []
        ctrl = MeshtasticController()
        raw_pkts = []
        ctrl.raw_packet.connect(raw_pkts.append)

        with _patch_ble(fakes):
            ctrl.connect_ble("AA:BB:CC:DD:EE:FF")
            _pump_events(1000)
            assert fakes, "No fake interface was created"
            fakes[0].pump_text("raw test")
            _pump_events(500)

        assert len(raw_pkts) >= 1, "raw_packet signal not emitted"
        ctrl.shutdown()


# ── Send text ──────────────────────────────────────────────────────────────────

class TestSendText:
    def test_send_text_calls_interface(self):
        fakes = []
        ctrl = MeshtasticController()

        with _patch_ble(fakes):
            ctrl.connect_ble("AA:BB:CC:DD:EE:FF")
            _pump_events(1000)
            ctrl.send_channel_text("Hello world", channel_index=0, local_id="local-1")
            _pump_events(500)

        assert fakes, "No fake interface was created"
        assert any(s["text"] == "Hello world" for s in fakes[0].sent_texts), (
            f"Text not sent; sent_texts={fakes[0].sent_texts}"
        )
        ctrl.shutdown()


# ── Connection lost ────────────────────────────────────────────────────────────

class TestConnectionLost:
    def test_connection_lost_closes_the_interface(self):
        # _on_connection_lost used to set state/emit signals but never call
        # _close_interface() — every other path that ends a connection
        # does. The dead interface's background reader thread and socket/
        # handle would otherwise stay alive and leaking until the user
        # happened to click Connect or Disconnect again.
        from pubsub import pub

        fakes = []
        ctrl = MeshtasticController()

        with _patch_ble(fakes):
            ctrl.connect_ble("AA:BB:CC:DD:EE:FF")
            _pump_events(1000)
            assert fakes, "No fake interface was created"
            assert not fakes[0]._closed

            pub.sendMessage("meshtastic.connection.lost", interface=fakes[0])
            _pump_events(500)

        assert fakes[0]._closed, "Interface was not closed on connection.lost"
        assert ctrl._worker._interface is None
        ctrl.shutdown()
