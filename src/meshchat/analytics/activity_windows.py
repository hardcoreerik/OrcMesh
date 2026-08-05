"""MeshChat – analytics: activity window helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def active_cutoff(minutes: int = 60) -> datetime:
    """Return the UTC datetime that marks the start of the active window."""
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def is_active(last_heard: datetime | None, window_minutes: int = 60) -> bool:
    """Return True if last_heard is within the activity window."""
    if last_heard is None:
        return False
    cutoff = active_cutoff(window_minutes)
    lh = last_heard
    if lh.tzinfo is None:
        lh = lh.replace(tzinfo=timezone.utc)
    return lh >= cutoff


def relative_age(dt: datetime | None) -> str:
    """Return a human-readable relative age string."""
    if dt is None:
        return "Never"
    now = datetime.now(timezone.utc)
    target = dt
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = now - target
    secs = int(delta.total_seconds())
    if secs < 0:
        return "Just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def staleness_state(
    last_seen: datetime | None,
    expected_interval_s: float = 900,
) -> str:
    """
    Returns 'fresh', 'stale', or 'old' based on spec:
    - fresh  : <= 2x expected interval
    - stale  : > 2x expected interval
    - old    : > 24 hours
    """
    if last_seen is None:
        return "old"
    now = datetime.now(timezone.utc)
    ts = last_seen
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_s = (now - ts).total_seconds()
    if age_s > 86400:
        return "old"
    if age_s > 2 * expected_interval_s:
        return "stale"
    return "fresh"
