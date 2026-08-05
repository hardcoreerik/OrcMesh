"""MeshChat – analytics: hop metrics calculations."""
from __future__ import annotations


def compute_hops_used(hop_start: int | None, hop_limit: int | None) -> int | None:
    """
    Compute hops used from hop_start and hop_limit.

    Rules:
    - hop_start == 0 → unknown (older firmware, not guaranteed direct)
    - hop_limit > hop_start → invalid
    - Either field None → unknown
    Returns None when the result is unknown/invalid.
    """
    if hop_start is None or hop_limit is None:
        return None
    if hop_start == 0:
        return None  # unknown, not guaranteed direct
    if hop_limit > hop_start:
        return None  # invalid field values
    return hop_start - hop_limit


def is_direct_rf(
    hops_used: int | None,
    hop_start: int | None,
    via_mqtt: bool | None,
) -> bool:
    """
    Returns True only when a packet is a confirmed zero-hop RF packet.

    A packet is direct RF when:
    - hops_used == 0
    - hop_start is trustworthy (> 0)
    - via_mqtt is not True
    """
    return (
        hops_used == 0
        and hop_start is not None
        and hop_start > 0
        and via_mqtt is not True
    )


def percent_hops_used(
    hops_used: int | None, hop_start: int | None
) -> float | None:
    """Return fraction of hop allowance consumed, or None if unknown."""
    if hops_used is None or hop_start is None or hop_start == 0:
        return None
    return hops_used / hop_start


def hop_efficiency_bucket(percent: float | None) -> str:
    """Classify percent-hops-used into a display bucket."""
    if percent is None:
        return "Unknown"
    if percent == 0.0:
        return "0%"
    if percent <= 0.25:
        return "1–24%"
    if percent <= 0.50:
        return "25–49%"
    if percent <= 0.75:
        return "50–74%"
    if percent < 1.0:
        return "75–99%"
    return "100%"
