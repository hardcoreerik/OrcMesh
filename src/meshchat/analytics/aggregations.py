"""MeshChat – analytics: aggregation helpers."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def count_by_category(items: list[Any], key_fn) -> dict[str, int]:
    """
    Count items grouped by a key function.
    Returns a dict sorted by count descending.
    """
    counter: Counter = Counter()
    for item in items:
        k = key_fn(item)
        counter[k] += 1
    return dict(counter.most_common())


def top_n(
    items: list[Any],
    key_fn,
    n: int = 10,
    reverse: bool = True,
) -> list[Any]:
    """Return top-n items sorted by key_fn."""
    return sorted(items, key=key_fn, reverse=reverse)[:n]


def group_by(items: list[Any], key_fn) -> dict[Any, list[Any]]:
    """Group items by a key function into a dict of lists."""
    result: dict[Any, list[Any]] = defaultdict(list)
    for item in items:
        result[key_fn(item)].append(item)
    return dict(result)


def role_group(role: str | None) -> str:
    """Classify a Meshtastic role into 'Infrastructure' or 'Client-like'."""
    infrastructure = {
        "ROUTER", "ROUTER_LATE", "CLIENT_BASE", "ROUTER_CLIENT", "REPEATER"
    }
    if role in infrastructure:
        return "Infrastructure"
    if role:
        return "Client-like"
    return "Unknown"


def friendly_hw_name(hw_model: str | None) -> str:
    """Convert a hardware enum string to a readable display name."""
    if not hw_model:
        return "Unknown"
    # Convert SNAKE_CASE → Title Case with common replacements
    name = hw_model.replace("_", " ").title()
    replacements = {
        "Tbeam": "T-Beam",
        "Tlora": "T-Lora",
        "Heltec": "Heltec",
        "Seeed": "Seeed",
        "Rak": "RAK",
    }
    for old, new in replacements.items():
        name = name.replace(old, new)
    return name
