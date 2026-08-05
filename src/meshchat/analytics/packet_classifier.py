"""MeshChat – analytics: packet type classification."""
from __future__ import annotations

# Official Meshtastic PortNum values
PORTNUM_LABELS: dict[int, str] = {
    0: "Unknown Port",
    1: "Text",
    3: "Position",
    4: "Node Info",
    5: "Routing",
    67: "Telemetry",
    70: "Traceroute",
    71: "Neighbor Info",
    73: "Map Report",
    256: "Private App",
}

# Grouped classification
_KNOWN_PORTS = set(PORTNUM_LABELS.keys()) - {0, 256}


def classify_portnum(portnum: int | None) -> str:
    """Return a display label for a port number."""
    if portnum is None:
        return "Unknown Port"
    return PORTNUM_LABELS.get(portnum, f"Other Known ({portnum})")


def packet_category(portnum: int | None, pki_encrypted: bool | None = None) -> str:
    """
    Return the Packet Breakdown category for a packet.

    Categories (matching spec section 32):
    Text | Position | Node Info | Routing | Telemetry | Traceroute |
    Neighbor Info | Private App | Map Report | Other Known |
    Unknown Port | Encrypted/Undecoded
    """
    if pki_encrypted:
        return "Encrypted/Undecoded"
    if portnum is None:
        return "Unknown Port"
    mapping = {
        1: "Text",
        3: "Position",
        4: "Node Info",
        5: "Routing",
        67: "Telemetry",
        70: "Traceroute",
        71: "Neighbor Info",
        73: "Map Report",
        256: "Private App",
    }
    if portnum in mapping:
        return mapping[portnum]
    if portnum == 0:
        return "Unknown Port"
    return "Other Known"


def is_text_packet(portnum: int | None) -> bool:
    return portnum == 1


def is_position_packet(portnum: int | None) -> bool:
    return portnum == 3


def is_telemetry_packet(portnum: int | None) -> bool:
    return portnum == 67


def is_nodeinfo_packet(portnum: int | None) -> bool:
    return portnum == 4
