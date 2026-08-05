"""MeshChat for Windows – TelemetrySample model."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TelemetrySample:
    """Normalized telemetry observation from a node."""

    node_num: int
    observed_at: datetime
    telemetry_time: datetime | None = None

    # Device metrics
    battery_level: float | None = None      # 0-100 %
    voltage: float | None = None            # Volts
    channel_utilization: float | None = None  # 0-100 %
    air_util_tx: float | None = None        # 0-100 %

    # Environment metrics
    temperature_c: float | None = None
    relative_humidity: float | None = None  # 0-100 %
    barometric_pressure_hpa: float | None = None
    gas_resistance_ohm: float | None = None

    # Power metrics
    current_a: float | None = None
    power_w: float | None = None

    # Health metrics
    heart_rate_bpm: float | None = None
    spo2_percent: float | None = None
    body_temperature_c: float | None = None

    @property
    def temperature_f(self) -> float | None:
        if self.temperature_c is None:
            return None
        return self.temperature_c * 9 / 5 + 32

    @property
    def gas_resistance_label(self) -> str | None:
        if self.gas_resistance_ohm is None:
            return None
        ohm = self.gas_resistance_ohm
        if ohm >= 1_000_000:
            return f"{ohm / 1_000_000:.1f} MΩ"
        if ohm >= 1_000:
            return f"{ohm / 1_000:.1f} kΩ"
        return f"{ohm:.0f} Ω"
