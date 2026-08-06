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

    def test_stale_connection_lost_is_a_complete_no_op_after_a_reconnect(self):
        # meshtastic.connection.lost is published on the meshtastic
        # library's own background thread, not this worker's thread. A
        # naive "check identity, then act" sequence has a window where a
        # reconnect can install a replacement interface between the check
        # and the state-change/close/signals — which would then tear down
        # or misreport the live (replacement) connection instead of the
        # dead one the event is actually about.
        # _claim_interface_if (see meshtastic_controller.py) closes that
        # window by making the check-and-clear atomic under a lock. This
        # simulates the event arriving for an interface that a reconnect
        # has already fully replaced: EVERY side effect — state change,
        # close, and both signals — must be skipped, not just the close.
        from meshchat.controllers.meshtastic_controller import ConnectionState, MeshtasticWorker
        from tests.fakes.fake_meshtastic_interface import FakeMeshtasticInterface

        worker = MeshtasticWorker()
        old_iface = FakeMeshtasticInterface()
        new_iface = FakeMeshtasticInterface()
        worker._interface = new_iface
        worker._state = ConnectionState.CONNECTED

        states = []
        disconnected = []
        errors = []
        worker._set_state = lambda *a, **k: states.append((a, k))
        worker.disconnected.connect(disconnected.append)
        worker.error_occurred.connect(errors.append)

        worker._on_connection_lost(interface=old_iface)

        assert states == [], "Stale connection.lost must not change state"
        assert disconnected == [], "Stale connection.lost must not emit disconnected"
        assert errors == [], "Stale connection.lost must not emit an error"
        assert not new_iface._closed, "Replacement interface must not be closed"
        assert not old_iface._closed, "Stale event only ever no-ops, never closes anything"
        assert worker._interface is new_iface

    def test_reconnect_during_close_suppresses_the_stale_error_report(self):
        # A legitimately claimed loss event (old_iface really was the
        # active interface when the event fired) still shouldn't report
        # ERROR if, by the time interface.close() returns, a reconnect has
        # already installed and is using a new interface — close() can
        # block on I/O, and Connect is allowed again as soon as state is
        # ERROR. Simulate the reconnect landing during close() itself, the
        # same way a real connect_ble/tcp/serial does: _close_interface()
        # first (bumps the connect generation — the claimed report compares
        # against this), then _set_interface() once the new interface is
        # constructed.
        from meshchat.controllers.meshtastic_controller import MeshtasticWorker
        from tests.fakes.fake_meshtastic_interface import FakeMeshtasticInterface

        worker = MeshtasticWorker()
        old_iface = FakeMeshtasticInterface()
        new_iface = FakeMeshtasticInterface()
        worker._interface = old_iface

        orig_close = old_iface.close

        def _close_and_reconnect():
            orig_close()
            worker._close_interface()  # reconnect's own teardown-of-prior step
            worker._set_interface(new_iface)  # ...then the new interface lands

        old_iface.close = _close_and_reconnect

        states = []
        disconnected = []
        errors = []
        worker._set_state = lambda *a, **k: states.append((a, k))
        worker.disconnected.connect(disconnected.append)
        worker.error_occurred.connect(errors.append)

        worker._on_connection_lost(interface=old_iface)

        assert old_iface._closed
        assert states == [], "Must not report ERROR once a successor interface exists"
        assert disconnected == [], "Must not emit disconnected for a superseded connection"
        assert errors == [], "Must not emit an error for a superseded connection"
        assert worker._interface is new_iface, "The reconnect's interface must survive untouched"

    def test_reconnect_still_mid_connect_suppresses_the_stale_error_report(self):
        # Same as above, but the reconnect's own interface constructor
        # hasn't returned yet when close() finishes — self._interface is
        # still None at that instant (only _close_interface() ran, not yet
        # _set_interface()). A plain "self._interface is not None" gate
        # would miss this and misreport a live-in-progress reconnect as
        # lost; the generation counter (bumped by _close_interface() even
        # when there was nothing to close) catches it regardless.
        from meshchat.controllers.meshtastic_controller import MeshtasticWorker
        from tests.fakes.fake_meshtastic_interface import FakeMeshtasticInterface

        worker = MeshtasticWorker()
        old_iface = FakeMeshtasticInterface()
        worker._interface = old_iface

        orig_close = old_iface.close

        def _close_and_start_reconnecting():
            orig_close()
            worker._close_interface()  # reconnect's teardown step only —
            # the new interface's constructor is still "in flight" here

        old_iface.close = _close_and_start_reconnecting

        states = []
        disconnected = []
        errors = []
        worker._set_state = lambda *a, **k: states.append((a, k))
        worker.disconnected.connect(disconnected.append)
        worker.error_occurred.connect(errors.append)

        worker._on_connection_lost(interface=old_iface)

        assert worker._interface is None, "Sanity check: mid-connect, no interface yet"
        assert states == [], "Must not report ERROR for a reconnect still in flight"
        assert disconnected == []
        assert errors == []
