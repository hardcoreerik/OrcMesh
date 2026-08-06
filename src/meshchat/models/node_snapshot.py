"""MeshChat for Windows – NodeSnapshot model."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class NodeSnapshot:
    """Live snapshot of a node as observed in the current session."""

    node_num: int
    node_id: str | None = None
    long_name: str | None = None
    short_name: str | None = None
    role: str | None = None
    hw_model: str | None = None

    first_seen: datetime | None = None
    last_heard: datetime | None = None

    packet_count: int = 0
    text_count: int = 0
    position_count: int = 0
    telemetry_count: int = 0

    last_snr: float | None = None
    last_rssi: int | None = None
    last_hops_used: int | None = None
    last_hop_start: int | None = None
    last_via_mqtt: bool | None = None

    via_mqtt_count: int = 0
    rf_count: int = 0

    # Most recent telemetry reading (device + environment metrics), if any.
    last_telemetry_at: datetime | None = None
    battery_level: float | None = None
    voltage: float | None = None
    channel_utilization: float | None = None
    air_util_tx: float | None = None
    temperature_c: float | None = None
    relative_humidity: float | None = None
    barometric_pressure_hpa: float | None = None

    @property
    def display_name(self) -> str:
        return self.long_name or self.short_name or self.node_id or f"!{self.node_num:08x}"

    @property
    def short_display(self) -> str:
        return self.short_name or (self.node_id[-4:] if self.node_id else f"{self.node_num:04x}")

    @property
    def is_direct(self) -> bool:
        """Most recent packet was a direct zero-hop RF packet.

        Mirrors analytics.hop_metrics.is_direct_rf / NetworkPacket.is_direct_rf
        — a zero-hop packet relayed over MQTT is not a direct RF contact, it
        just preserved the original hop fields across the bridge.
        """
        return (
            self.last_hops_used == 0
            and self.last_hop_start is not None
            and self.last_hop_start > 0
            and self.last_via_mqtt is not True
        )
