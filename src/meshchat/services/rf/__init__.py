"""The RF device layer: what is attached, and how each thing is identified.

Named ``rf`` rather than ``sdr`` because the layer has to cover receivers that are not
SDRs in the usual sense — a Meshtastic radio is a receiver too, and it reports protocol
truth rather than samples.

Nothing here imports Qt, and **nothing here opens a device**: discovery is listing only,
for the same reason ``rtl_test -t`` enumerates dongles instead of opening one. Claiming a
receiver takes seconds and fails while something else holds it, so a listing that
claimed one would be unusable as a refresh.

Scope so far is identity, transport and capability. The backends that actually stream — and
the block accounting that makes their degradation visible — come next; see
``docs/rf-observatory/PHASE-0-AUDIT.md``.
"""
from __future__ import annotations

from .base import (
    ETHERNET,
    LOCAL,
    MEASURED_LIMITS,
    PREFERENCE,
    USB,
    USB_GADGET,
    MeasuredLimit,
    RfCapabilities,
    RfDeviceInfo,
    RfTransport,
)
from .capabilities import parse_range, parse_sample_format, probe_libiio
from .registry import IioContext, discover, parse_contexts, pluto_devices, rtl_devices

__all__ = [
    "ETHERNET",
    "LOCAL",
    "MEASURED_LIMITS",
    "PREFERENCE",
    "USB",
    "USB_GADGET",
    "IioContext",
    "MeasuredLimit",
    "RfCapabilities",
    "RfDeviceInfo",
    "RfTransport",
    "discover",
    "parse_contexts",
    "parse_range",
    "parse_sample_format",
    "pluto_devices",
    "probe_libiio",
    "rtl_devices",
]
