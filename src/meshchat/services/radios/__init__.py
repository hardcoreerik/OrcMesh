"""The radio device layer: identity, enumeration, and who holds what.

Separate from `services/rf/`, which is the SDR side of the same observatory. The two do
not overlap: an SDR receives, a Meshtastic radio is a peer on a mesh. They share a shape
— identify a device, enumerate without opening it, lease it to one owner — because both
run into the same class of problem: one physical device reachable more than one way, with
the operating system protecting only one of those ways.
"""
from __future__ import annotations

from .base import (
    BLE,
    PREFERENCE,
    SERIAL,
    TCP,
    UNRANKED,
    RadioCandidate,
    RadioTransport,
    candidate_key,
    look_like_mac,
    mac_family,
    transport_rank,
)
from .lease import (
    RadioBusy,
    acquire_radio,
    add_identity,
    held_radios,
    hold,
    owner_radios,
    radio_owner,
    release_radio,
)
from .registry import (
    MESHTASTIC_USB_IDS,
    BleAdvertisement,
    Candidate,
    PortSummary,
    candidates,
    ports_from_comports,
)

__all__ = [
    "BLE",
    "MESHTASTIC_USB_IDS",
    "PREFERENCE",
    "SERIAL",
    "TCP",
    "UNRANKED",
    "BleAdvertisement",
    "Candidate",
    "PortSummary",
    "RadioBusy",
    "RadioCandidate",
    "RadioTransport",
    "acquire_radio",
    "add_identity",
    "candidate_key",
    "candidates",
    "held_radios",
    "hold",
    "look_like_mac",
    "mac_family",
    "owner_radios",
    "ports_from_comports",
    "radio_owner",
    "release_radio",
    "transport_rank",
]
