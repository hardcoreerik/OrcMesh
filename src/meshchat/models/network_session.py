"""MeshChat for Windows – NetworkSession model."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class NetworkSession:
    """Represents one monitoring session (from connect to disconnect/reset)."""

    transport: str
    connection_target: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    local_node_num: int | None = None
    local_node_id: str | None = None
    meshtastic_version: str | None = None
    firmware_version: str | None = None
    packet_count: int = 0

    @property
    def elapsed(self) -> float:
        """Elapsed seconds since session start."""
        end = self.ended_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    def format_elapsed(self) -> str:
        s = int(self.elapsed)
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        if s < 86400:
            h, rem = divmod(s, 3600)
            return f"{h}h {rem // 60}m"
        d, rem = divmod(s, 86400)
        return f"{d}d {rem // 3600}h"

    def end(self) -> None:
        self.ended_at = datetime.now(timezone.utc)

    def start_new(self) -> None:
        """Reinitialize in place for a fresh connect-to-disconnect run.

        Mutates rather than replacing the object: PacketIngestor holds this
        same NetworkSession instance and mutates it directly (packet_count
        += 1, session_id stamped on packets/messages) — replacing the
        reference here wouldn't be visible there without also threading a
        setter through. Reconnecting without this call previously meant
        every connect-to-disconnect run in one app session shared the same
        session_id, since __init__ only creates the session once.
        """
        self.id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc)
        self.ended_at = None
        self.packet_count = 0

    @classmethod
    def new(cls, transport: str = "unknown", connection_target: str = "") -> "NetworkSession":
        return cls(transport=transport, connection_target=connection_target)
