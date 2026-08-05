"""MeshChat for Windows – PositionSample model."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PositionSample:
    """A single position observation for a node."""

    node_num: int
    observed_at: datetime
    latitude: float | None
    longitude: float | None
    altitude_m: float | None = None
    speed_ms: float | None = None
    heading_deg: float | None = None
    position_time: datetime | None = None
    precision_bits: int | None = None
    sats_in_view: int | None = None

    @property
    def is_valid(self) -> bool:
        """A position is valid when coordinates are present and plausible."""
        return (
            self.latitude is not None
            and self.longitude is not None
            and -90.0 <= self.latitude <= 90.0
            and -180.0 <= self.longitude <= 180.0
        )

    @property
    def coords(self) -> tuple[float, float] | None:
        if self.is_valid:
            return (self.latitude, self.longitude)  # type: ignore[return-value]
        return None
