"""Identity for the Meshtastic radios, independent of how they are reached.

One physical radio is usually reachable more than one way, and the operating system
only protects *one* of those ways. Measured on this bench (2026-09-27):

* The Heltec V4 reports serial number ``44:1B:F6:6F:81:BC`` on ``COM24`` and
  advertises the BLE address ``44:1B:F6:6F:81:BD``. Those are two handles onto **one**
  radio, and the port lock knows nothing about the second one.
* Opening the same port twice **is** refused — ``PermissionError(13, 'Access is
  denied.')`` — so the OS protects the port, and only the port.

That asymmetry is what this module exists for. A lease keyed on a port would let a
serial connection and a BLE connection to the same radio run at once, which is not two
radios; it is one radio being configured from two directions.

Everything here is pure. Nothing opens a port, and nothing talks to a device — for a
measured reason as well as a design one: one of the Espressif USB devices attached to
this machine is *not* a radio, and enumerating by probing would hang on it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: How a radio is reached. Kept as plain strings rather than an Enum because these
#: travel into settings files and log lines, where a string is what gets read back.
SERIAL = "serial"
TCP = "tcp"
BLE = "ble"

#: Order to reach a radio in when more than one transport is available.
#:
#: Serial leads because it is the only path measured solid on this bench: the Heltec
#: held a session while a second radio was opened, configured and closed. BLE needs the
#: radio paired with Windows *first* — an unpaired link is refused at the first write
#: with a message about a PIN — and a BLE connect takes ~23 s warm, ~35 s the first
#: time, against 13.6 s measured for serial. TCP is for a radio on the network and is
#: placed in the middle on the strength of needing no port at all; it has **not** been
#: tested on this bench, so it is deliberately not ranked first on hope.
PREFERENCE: dict[str, int] = {SERIAL: 0, TCP: 1, BLE: 2}

#: Rank given to a transport this code has never heard of. Last, not first: an unknown
#: path is not a better path.
UNRANKED = 99

_MAC_SEPARATED = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")
_MAC_BARE = re.compile(r"^[0-9A-Fa-f]{12}$")


def transport_rank(kind: str) -> int:
    return PREFERENCE.get(kind, UNRANKED)


def normalise_mac(text: str) -> str | None:
    """A hardware address in one canonical form, or None if this is not one.

    Two forms are needed because the two radios on this bench report differently, and
    assuming one form is how this got written wrong the first time:

    * the Heltec reports ``44:1B:F6:6F:81:BC`` over serial — colon-separated;
    * the T-Beam reports ``48CA435BAA2C`` — the same twelve hex digits with no separators.

    The bare form was missed at first, so the T-Beam was not recognised as having a
    hardware address at all: it was listed twice, once per transport, and handed a note
    saying it could not be matched when it could.
    """
    stripped = text.strip()
    if _MAC_SEPARATED.match(stripped):
        octets = re.split(r"[:-]", stripped)
    elif _MAC_BARE.match(stripped):
        octets = [stripped[i : i + 2] for i in range(0, 12, 2)]
    else:
        return None
    return ":".join(octet.upper() for octet in octets)


def look_like_mac(text: str) -> bool:
    return normalise_mac(text) is not None


def mac_family(address: str) -> str | None:
    """The part of a hardware address this code treats as identifying one board.

    The last octet's low bit is masked off, because an Espressif board advertises its BLE
    address as its base MAC plus one. Measured on **both** radios on this bench, from the
    serial numbers against the advertisements:

    * ``44:1B:F6:6F:81:BC`` on COM24, advertised as ``44:1B:F6:6F:81:BD`` as ``hrdc_81bc``;
    * ``48CA435BAA2C`` on COM16, advertised as ``48:CA:43:5B:AA:2D`` as ``HcMe_aa2c``.

    Two boards, the same +1 offset, so this is a convention rather than a coincidence.
    Two observations is still not a specification, and the rule stays a *hint* for not
    opening the same radio twice — never a conclusion about identity. A radio's identity
    is its node number, and that only comes from talking to it. If a board ever shows a
    different offset, this is the function to revisit.
    """
    canonical = normalise_mac(address)
    if canonical is None:
        return None
    octets = canonical.split(":")
    octets[-1] = f"{int(octets[-1], 16) & 0xFE:02X}"
    return ":".join(octets)


@dataclass(frozen=True)
class RadioTransport:
    """One way of reaching a radio."""

    kind: str
    address: str
    #: What to show a user, when the address alone is not it (``COM24`` is, a MAC less so).
    label: str = ""

    @property
    def rank(self) -> int:
        return transport_rank(self.kind)

    def describe(self) -> str:
        return self.label or f"{self.kind}: {self.address}"


@dataclass(frozen=True)
class RadioCandidate:
    """A radio found, before anything has been asked of it.

    Deliberately not called discovered: nothing here has been contacted, so the name and
    node number are unknown and a transport list may be incomplete. It exists to tell a
    user what is attached without opening ports to find out.
    """

    key: str
    transports: tuple[RadioTransport, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    def streaming_transport(self) -> RadioTransport | None:
        """The transport to use, or None if there is none to choose from."""
        usable = [t for t in self.transports if t.rank != UNRANKED]
        if not usable:
            return None
        return min(usable, key=lambda t: t.rank)

    def describe(self) -> str:
        if not self.transports:
            return self.key
        return f"{self.key} via " + ", ".join(t.describe() for t in self.transports)


def candidate_key(*, node_num: int | None = None, address: str | None = None) -> str:
    """The identity to hold a radio under.

    A node number wins whenever there is one: it is the mesh's own name for the radio,
    assigned by the radio itself and agreed by every node that hears it. A hardware
    address is the fallback, because it is all that is known before anything is asked.
    """
    if node_num is not None:
        return f"radio:{node_num}"
    family = mac_family(address or "")
    if family is not None:
        return f"radio:{family}"
    return f"radio:{address}" if address else "radio:unknown"
