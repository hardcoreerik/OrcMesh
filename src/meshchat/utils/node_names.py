"""MeshChat – utility: node name resolution."""
from __future__ import annotations


def resolve_node_name(
    node_num: int | None,
    node_id: str | None = None,
    nodes_by_num: dict | None = None,
) -> str:
    """
    Resolve a node display name using the priority order from spec:
    1. NodeDB longName
    2. NodeDB shortName
    3. packet fromId
    4. formatted node number
    5. "Unknown node"
    """
    if nodes_by_num and node_num and node_num in nodes_by_num:
        user = nodes_by_num[node_num].get("user", {})
        long_name = user.get("longName") or user.get("long_name")
        if long_name:
            return long_name
        short_name = user.get("shortName") or user.get("short_name")
        if short_name:
            return short_name

    if node_id:
        return node_id

    if node_num is not None:
        return f"!{node_num:08x}"

    return "Unknown node"


def resolve_short_name(
    node_num: int | None,
    node_id: str | None = None,
    nodes_by_num: dict | None = None,
) -> str:
    """Return the shortest useful name for a node."""
    if nodes_by_num and node_num and node_num in nodes_by_num:
        user = nodes_by_num[node_num].get("user", {})
        short = user.get("shortName") or user.get("short_name")
        if short:
            return short

    if node_id:
        return node_id[-4:] if len(node_id) > 4 else node_id

    if node_num is not None:
        return f"{node_num:04x}"

    return "????"
