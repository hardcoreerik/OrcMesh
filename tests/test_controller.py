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
    def test_connection_lost_does_not_close_or_clear_the_interface(self):
        # meshtastic fires meshtastic.connection.lost, for the SAME live
        # interface object, on a routine post-config-change soft reboot —
        # not only on a real disconnect. It deliberately calls the base
        # MeshInterface._disconnected() (skipping the subclass override
        # that closes the transport), then immediately calls
        # _startConfig() to resync on that same interface, which fires
        # meshtastic.connection.established again once it completes.
        # Closing or clearing self._interface here would tear down a
        # connection that's still alive and about to recover, and would
        # break the recovery itself (the later connection.established
        # event's identity check would then fail). See ARCHITECTURE.md.
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

        assert not fakes[0]._closed, "connection.lost must not close the interface"
        assert ctrl._worker._interface is fakes[0], (
            "connection.lost must not clear self._interface — a soft-reboot "
            "resync on the same interface needs it to still match"
        )
        ctrl.shutdown()

    def test_connection_lost_still_reports_state_and_signals(self):
        from meshchat.controllers.meshtastic_controller import MeshtasticWorker
        from tests.fakes.fake_meshtastic_interface import FakeMeshtasticInterface

        worker = MeshtasticWorker()
        iface = FakeMeshtasticInterface()
        worker._interface = iface

        states = []
        disconnected = []
        errors = []
        worker._set_state = lambda *a, **k: states.append((a, k))
        worker.disconnected.connect(disconnected.append)
        worker.error_occurred.connect(errors.append)

        worker._on_connection_lost(interface=iface)

        assert len(states) == 1
        assert len(disconnected) == 1
        assert len(errors) == 1

    def test_stale_connection_lost_for_a_replaced_interface_is_ignored(self):
        # An event for an interface that's no longer self._interface (the
        # user has since disconnected and connected to a different radio)
        # must not report anything for the new, unrelated connection.
        from meshchat.controllers.meshtastic_controller import MeshtasticWorker
        from tests.fakes.fake_meshtastic_interface import FakeMeshtasticInterface

        worker = MeshtasticWorker()
        old_iface = FakeMeshtasticInterface()
        new_iface = FakeMeshtasticInterface()
        worker._interface = new_iface

        states = []
        disconnected = []
        errors = []
        worker._set_state = lambda *a, **k: states.append((a, k))
        worker.disconnected.connect(disconnected.append)
        worker.error_occurred.connect(errors.append)

        worker._on_connection_lost(interface=old_iface)

        assert states == []
        assert disconnected == []
        assert errors == []
        assert worker._interface is new_iface

    def test_soft_reboot_resync_restores_connected_on_the_same_interface(self):
        # The actual scenario this behavior exists for: connection.lost
        # fires for the live interface (soft reboot), then
        # connection.established fires again for that SAME interface once
        # _startConfig() finishes resyncing. Both events must be accepted
        # since self._interface was never touched by the first one.
        from pubsub import pub

        fakes = []
        ctrl = MeshtasticController()
        states = []
        ctrl.connection_state_changed.connect(lambda s, d: states.append(s))

        with _patch_ble(fakes):
            ctrl.connect_ble("AA:BB:CC:DD:EE:FF")
            _pump_events(1000)
            assert fakes, "No fake interface was created"

            pub.sendMessage("meshtastic.connection.lost", interface=fakes[0])
            _pump_events(200)
            pub.sendMessage("meshtastic.connection.established", interface=fakes[0])
            _pump_events(500)

        assert ConnectionState.ERROR in states, f"States: {states}"
        assert ConnectionState.CONNECTED in states, f"States: {states}"
        assert states[-1] == ConnectionState.CONNECTED, (
            f"Resync on the same interface must restore CONNECTED, got: {states}"
        )
        ctrl.shutdown()
