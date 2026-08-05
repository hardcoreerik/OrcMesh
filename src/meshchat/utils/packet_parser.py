"""MeshChat – utility: defensive Meshtastic packet parser."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedTextMessage:
    packet_id: int | None
    sender_num: int | None
    sender_id: str | None
    sender_long_name: str | None
    sender_short_name: str | None
    channel_index: int
    rx_time: datetime | None
    rx_snr: float | None
    rx_rssi: int | None
    hop_start: int | None
    hop_limit: int | None
    hops_used: int | None
    text: str


def _extract_text(decoded: dict) -> str | None:
    """Try to get text from decoded dict, falling back to payload bytes."""
    text = decoded.get("text")
    if isinstance(text, str):
        return text

    payload = decoded.get("payload")
    if isinstance(payload, bytes):
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(payload, str):
        return payload

    return None


def _get(pkt: dict, *keys: str, default=None):
    """Try multiple key names (camelCase and snake_case)."""
    for k in keys:
        if k in pkt:
            return pkt[k]
    return default


def parse_text_packet(
    packet: dict,
    nodes_by_num: dict | None = None,
) -> ParsedTextMessage | None:
    """
    Defensively parse a Meshtastic text packet dict.

    Returns None when the packet cannot be parsed into a valid text message.
    """
    try:
        decoded = packet.get("decoded", {})
        if not isinstance(decoded, dict):
            return None

        text = _extract_text(decoded)
        if text is None:
            return None

        # Sender resolution
        sender_num = _get(packet, "from", "fromNum")
        sender_id = _get(packet, "fromId")
        sender_long = None
        sender_short = None

        if nodes_by_num and sender_num and sender_num in nodes_by_num:
            user = nodes_by_num[sender_num].get("user", {})
            sender_long = user.get("longName") or user.get("long_name")
            sender_short = user.get("shortName") or user.get("short_name")
            if not sender_id:
                sender_id = user.get("id")

        # Hop info
        hop_start = _get(packet, "hopStart", "hop_start")
        hop_limit = _get(packet, "hopLimit", "hop_limit")
        hops_used: int | None = None
        if (
            hop_start is not None
            and hop_limit is not None
            and hop_start > 0
            and hop_limit <= hop_start
        ):
            hops_used = hop_start - hop_limit

        # Timestamps
        rx_ts = _get(packet, "rxTime", "rx_time")
        rx_time: datetime | None = None
        if rx_ts:
            try:
                rx_time = datetime.fromtimestamp(rx_ts, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                pass

        return ParsedTextMessage(
            packet_id=_get(packet, "id"),
            sender_num=sender_num,
            sender_id=sender_id,
            sender_long_name=sender_long,
            sender_short_name=sender_short,
            channel_index=int(_get(packet, "channel", "channelIndex") or 0),
            rx_time=rx_time,
            rx_snr=_get(packet, "rxSnr", "rx_snr"),
            rx_rssi=_get(packet, "rxRssi", "rx_rssi"),
            hop_start=hop_start,
            hop_limit=hop_limit,
            hops_used=hops_used,
            text=text,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("parse_text_packet failed: %s", exc)
        return None
