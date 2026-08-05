"""MeshChat – shared relative-time formatting.

Four near-identical copies of this logic had drifted apart: only one rolled
over to days, so a node last heard 134 days ago rendered as "134d" in the
Nodes table and "3227h" in the Last Heard ranking for the same underlying
value. One implementation, parameterised for the two presentation styles.
"""
from __future__ import annotations

from datetime import datetime, timezone

_MINUTE = 60
_HOUR = 3600
_DAY = 86400


def relative_age(
    ts: datetime | None,
    *,
    placeholder: str = "—",
    suffix: str = "",
) -> str:
    """Compact age of `ts`, e.g. "45s", "12m", "3h", "134d".

    placeholder: shown when `ts` is None.
    suffix:      appended to a real value, e.g. " ago".
    """
    if ts is None:
        return placeholder

    delta = (datetime.now(timezone.utc) - ts).total_seconds()
    if delta < 0:
        # Clock skew between this machine and the radio/mesh; treat a
        # future timestamp as "just now" rather than rendering a negative.
        delta = 0

    if delta < _MINUTE:
        value = f"{int(delta)}s"
    elif delta < _HOUR:
        value = f"{int(delta // _MINUTE)}m"
    elif delta < _DAY:
        value = f"{int(delta // _HOUR)}h"
    else:
        value = f"{int(delta // _DAY)}d"

    return f"{value}{suffix}"


def format_elapsed(seconds: int) -> str:
    """Session-duration style: "45s", "12m", "3h 20m", "2d 5h"."""
    if seconds < _MINUTE:
        return f"{seconds}s"
    if seconds < _HOUR:
        return f"{seconds // _MINUTE}m"
    if seconds < _DAY:
        return f"{seconds // _HOUR}h {(seconds % _HOUR) // _MINUTE}m"
    return f"{seconds // _DAY}d {(seconds % _DAY) // _HOUR}h"
