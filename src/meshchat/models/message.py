"""MeshChat for Windows – chat message models."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class MessageDirection(Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    SYSTEM = "system"


class MessageStatus(Enum):
    RECEIVED = "received"
    SENDING = "sending"
    ACCEPTED_BY_RADIO = "accepted_by_radio"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    UNKNOWN_DELIVERY = "unknown_delivery"


@dataclass
class ChatMessage:
    """A single chat message (inbound or outbound)."""

    channel_index: int
    direction: MessageDirection
    sender_name: str
    text: str
    status: MessageStatus
    local_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    packet_id: int | None = None
    sender_num: int | None = None
    sender_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    snr: float | None = None
    hop_count: int | None = None

    @property
    def is_outbound(self) -> bool:
        return self.direction == MessageDirection.OUTBOUND

    @property
    def is_system(self) -> bool:
        return self.direction == MessageDirection.SYSTEM

    def with_status(self, status: MessageStatus) -> "ChatMessage":
        """Return a copy with an updated status."""
        import copy
        msg = copy.copy(self)
        msg.status = status
        return msg
