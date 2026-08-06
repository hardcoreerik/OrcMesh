"""MeshChat – connection profile model."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConnectionProfile:
    """Last-used connection parameters, persisted across app restarts.

    Stored in MonitorStore's app_settings table under `connection.*` keys.
    One profile is kept; it is overwritten on every successful connection so
    the bar always reflects the most-recently-used settings.
    """

    transport: str              # "ble", "tcp", or "serial"
    ble_address: str = field(default="")
    tcp_host: str = field(default="")
    tcp_port: int = field(default=4403)
    serial_port: str = field(default="")

    @property
    def connection_target(self) -> str:
        """Human-readable target string, matches NetworkSession.connection_target."""
        if self.transport == "tcp":
            return f"{self.tcp_host}:{self.tcp_port}"
        if self.transport == "ble":
            return self.ble_address
        return self.serial_port
