"""MeshChat – ConnectionSupervisor: automatic reconnect with backoff.

Watches the controller for CONNECTION_LOST errors and schedules retries
with exponential backoff. Only fires after the radio was previously
connected at least once this session — transient failures during a fresh
manual connect are left to the user to retry, since auto-retry on a
mistyped host or unpaired device would be confusing.

The supervisor also persists the last-used connection profile to
MonitorStore so the ConnectionBar can be pre-filled on the next launch.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Slot

from meshchat.models.connection_profile import ConnectionProfile
from meshchat.services.monitor_store import MonitorStore

log = logging.getLogger(__name__)

#: Seconds to wait before successive reconnect attempts.  The last entry
#: is reused once exhausted (i.e. the supervisor keeps retrying at that
#: interval indefinitely until the user disconnects or it succeeds).
_BACKOFF_SECONDS = [2, 5, 15, 30, 60]


class ConnectionSupervisor(QObject):
    """Reconnect-with-backoff supervisor.

    Must be created on the GUI thread.  All slots and timers run on the
    GUI thread; reconnect calls are dispatched to the worker via queued
    connections through the controller.

    Lifecycle:
    - Call `set_profile(profile)` whenever the user initiates a connection
      so the supervisor knows what to reconnect to if the link drops.
    - Call `cancel()` when the user deliberately disconnects so the
      supervisor stops any pending retry.
    - The supervisor saves the profile to MonitorStore on every successful
      connect so it persists across restarts.
    """

    def __init__(self, controller, store: MonitorStore, parent=None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._store = store
        self._profile: ConnectionProfile | None = None
        self._attempt = 0
        self._was_connected = False
        self._active = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._attempt_reconnect)

        controller.connected.connect(self._on_connected)
        controller.disconnected.connect(self._on_disconnected)
        controller.error_occurred.connect(self._on_error)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_profile(self, profile: ConnectionProfile) -> None:
        """Record the profile for the connection that is about to start.

        Call this before every user-initiated connect so the supervisor
        knows what to reconnect to if the link drops later. The profile
        is saved to persistent storage the moment the connection succeeds.
        """
        self._profile = profile

    def cancel(self) -> None:
        """Stop any pending retry (user pressed Disconnect)."""
        self._active = False
        self._was_connected = False
        self._timer.stop()

    @staticmethod
    def load_profile(store: MonitorStore) -> ConnectionProfile | None:
        """Load the last-used profile from persistent storage.

        Returns None if no profile has been saved yet (first launch).
        """
        transport = store.get_setting("connection.transport")
        if not transport:
            return None
        try:
            tcp_port = int(store.get_setting("connection.tcp_port") or "4403")
        except ValueError:
            tcp_port = 4403
        return ConnectionProfile(
            transport=transport,
            ble_address=store.get_setting("connection.ble_address") or "",
            tcp_host=store.get_setting("connection.tcp_host") or "",
            tcp_port=tcp_port,
            serial_port=store.get_setting("connection.serial_port") or "",
        )

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    @Slot(object)
    def _on_connected(self, summary) -> None:
        self._timer.stop()
        self._active = False
        self._attempt = 0
        self._was_connected = True
        self._save_profile()

    @Slot(str)
    def _on_disconnected(self, reason: str) -> None:
        if "user" in reason.lower():
            self.cancel()

    @Slot(object)
    def _on_error(self, error) -> None:
        from meshchat.controllers.meshtastic_controller import ErrorCode
        if self._active:
            # A reconnect attempt itself failed — schedule the next one.
            self._attempt += 1
            self._schedule_retry()
        elif (
            error.code == ErrorCode.CONNECTION_LOST
            and self._was_connected
            and self._profile is not None
        ):
            # First loss after a good connection — start the retry sequence.
            self._active = True
            self._attempt = 0
            self._schedule_retry()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _schedule_retry(self) -> None:
        delay = _BACKOFF_SECONDS[min(self._attempt, len(_BACKOFF_SECONDS) - 1)]
        log.info(
            "ConnectionSupervisor: attempt %d, retrying in %ds",
            self._attempt + 1, delay,
        )
        self._controller.enter_reconnecting(f"Reconnecting… (attempt {self._attempt + 1})")
        self._timer.start(delay * 1000)

    @Slot()
    def _attempt_reconnect(self) -> None:
        if not self._active or self._profile is None:
            return
        p = self._profile
        log.info("ConnectionSupervisor: connecting via %s to %s", p.transport, p.connection_target)
        if p.transport == "tcp":
            self._controller.connect_tcp(p.tcp_host, p.tcp_port)
        elif p.transport == "serial":
            self._controller.connect_serial(p.serial_port)
        elif p.transport == "ble":
            self._controller.connect_ble(p.ble_address)

    def _save_profile(self) -> None:
        p = self._profile
        if p is None:
            return
        self._store.set_setting("connection.transport", p.transport)
        self._store.set_setting("connection.ble_address", p.ble_address)
        self._store.set_setting("connection.tcp_host", p.tcp_host)
        self._store.set_setting("connection.tcp_port", str(p.tcp_port))
        self._store.set_setting("connection.serial_port", p.serial_port)
