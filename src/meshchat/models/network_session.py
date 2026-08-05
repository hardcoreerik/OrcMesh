"""MeshChat for Windows – NetworkSession model."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class NetworkSession:
    """Represents one monitoring session.

    A session starts when the app connects (or the user presses Start New
    Session) and ends only on: an explicit End Session action, the app
    exiting cleanly, a database reset, or an optional long-disconnect
    policy — see the network-monitor spec (F:\\Ai\\MeshMonitor\\
    CLAUDE_MESHCHAT_NETWORK_MONITOR_ADDENDUM.md, "session lifecycle").
    A simple transport reconnect must NOT reset it — do not call `.end()`
    or otherwise rotate `.id` from a disconnect/reconnect handler.
    """

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
        """Explicitly end this session — call only from an actual session
        boundary (End Session action, DB reset), never from a plain
        connect/disconnect handler. See the class docstring."""
        self.ended_at = datetime.now(timezone.utc)

    @classmethod
    def new(cls, transport: str = "unknown", connection_target: str = "") -> "NetworkSession":
        return cls(transport=transport, connection_target=connection_target)
