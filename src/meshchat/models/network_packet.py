"""MeshChat for Windows – NetworkPacket normalized model."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NetworkPacket:
    """
    Normalized, immutable representation of a received Meshtastic packet.
    All thread boundaries use this type — never the raw dict.
    """

    session_id: str
    observed_at: datetime
    rx_time: datetime | None

    sender_num: int | None
    sender_id: str | None
    destination_num: int | None
    packet_id: int | None

    channel_index: int | None
    portnum: int | None
    portnum_name: str

    text: str | None
    payload_size: int | None

    rx_snr: float | None
    rx_rssi: int | None

    hop_start: int | None
    hop_limit: int | None
    hops_used: int | None  # computed: hop_start - hop_limit when valid

    via_mqtt: bool | None
    transport_mechanism: str | None
    pki_encrypted: bool | None

    want_ack: bool | None
    priority: str | None

    raw_metadata_json: str | None

    @property
    def is_direct_rf(self) -> bool:
        """
        Returns True only when we can confirm a zero-hop RF packet.
        hop_start==0 is treated as unknown (older firmware).
        """
        return (
            self.hops_used == 0
            and self.hop_start is not None
            and self.hop_start > 0
            and self.via_mqtt is not True
        )

    @property
    def is_via_mqtt(self) -> bool:
        return bool(self.via_mqtt)

    @property
    def portnum_label(self) -> str:
        return self.portnum_name or f"PORT_{self.portnum}"
