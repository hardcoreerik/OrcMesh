"""MeshChat – analytics: packet type classification."""
from __future__ import annotations

# Official Meshtastic PortNum values. Named so call sites read as intent
# ("is this a position packet?") rather than as bare magic numbers.
PORTNUM_TEXT = 1
PORTNUM_POSITION = 3
PORTNUM_NODEINFO = 4
PORTNUM_ROUTING = 5
PORTNUM_TELEMETRY = 67
PORTNUM_TRACEROUTE = 70
PORTNUM_NEIGHBORINFO = 71
PORTNUM_MAPREPORT = 73
PORTNUM_PRIVATE = 256

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
    return portnum == PORTNUM_TEXT


def is_position_packet(portnum: int | None) -> bool:
    return portnum == PORTNUM_POSITION


def is_telemetry_packet(portnum: int | None) -> bool:
    return portnum == PORTNUM_TELEMETRY


def is_nodeinfo_packet(portnum: int | None) -> bool:
    return portnum == PORTNUM_NODEINFO
