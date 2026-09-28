"""What a receiver is, and how it is identified.

No Qt and no I/O: these are the types a registry, a backend and eventually an orchestrator
pass between them. Keeping them apart from discovery is what lets the pure parts be tested
with no radio attached to anything.
"""
from __future__ import annotations

from dataclasses import dataclass

#: How a transport actually reaches the radio. The two ``ip:`` forms have to be told
#: apart: they are indistinguishable in a URI list and are 3x apart in measured
#: throughput, so treating them as one thing would silently choose the slow path.
ETHERNET = "ethernet"
USB_GADGET = "usb-gadget"
USB = "usb"
LOCAL = "local"

#: Preference for streaming, best first. Not a guess — it is the order the measurements in
#: ``docs/rf-observatory/PHASE-0-AUDIT.md`` support on this hardware:
#:
#: * ``ethernet``   ~57 MB/s peak, 15 MSPS lossless, 10 MSPS sustained for 60 s
#: * ``usb-gadget`` ~25 MB/s, 5 MSPS lossless, 5 MSPS sustained for 60 s
#: * ``usb``        marginally faster than the gadget in its one completed run, but it
#:                  wedged the board and dropped it off the bus **twice**, at lower rates
#:
#: Reliability deliberately outranks throughput: an unstable 30 MB/s is worth less than a
#: stable 25, because a transport that dies takes the capture with it.
PREFERENCE: dict[str, int] = {ETHERNET: 0, USB_GADGET: 1, LOCAL: 2, USB: 3}

#: The USB gadget's subnet, from the firmware's own default for ``ipaddr``. An ``ip:`` URI
#: inside it is the RNDIS gadget riding on USB, not a network socket — the same cable that
#: cannot carry more than ~25 MB/s.
GADGET_SUBNET_PREFIX = "192.168.2."

#: Ranks anything unrecognised after every known transport.
UNRANKED = 99


def transport_kind(uri: str) -> str:
    """Classify a libiio URI. Anything unrecognised is LOCAL, which ranks last."""
    if uri.startswith("usb:"):
        return USB
    if uri.startswith("ip:"):
        return USB_GADGET if uri[3:].startswith(GADGET_SUBNET_PREFIX) else ETHERNET
    return LOCAL


@dataclass(frozen=True)
class RfTransport:
    """One way to reach a device.

    A device is not its URI: the Pluto on this bench answers to three at once, and the
    point of this type is that a registry can hold all of them without inventing three
    receivers.
    """

    uri: str

    @property
    def kind(self) -> str:
        return transport_kind(self.uri)

    @property
    def rank(self) -> int:
        return PREFERENCE.get(self.kind, UNRANKED)


@dataclass(frozen=True)
class RfDeviceInfo:
    """One physical radio, however many ways there are to reach it."""

    #: Identity that survives a replug where the hardware allows one. For a Pluto that is
    #: the serial its firmware reports; for an RTL dongle it has to be the bus index,
    #: because this machine's two dongles both report ``SN: 00000001`` and no better
    #: discriminator exists.
    key: str
    backend: str
    label: str
    model: str = ""
    serial: str = ""
    rx: bool = True
    tx: bool = False
    transports: tuple[RfTransport, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def uris(self) -> tuple[str, ...]:
        return tuple(transport.uri for transport in self.transports)

    def streaming_transport(self) -> RfTransport | None:
        """The transport to prefer for a live stream, or None if there is no way in.

        Several transports to one radio is also a warning, not only a convenience: the
        tuner can only be held once, so a caller that streams over two of these is
        fighting itself.
        """
        if not self.transports:
            return None
        return min(self.transports, key=lambda transport: transport.rank)

    def describe(self) -> str:
        """One line naming the device and what it offers."""
        parts = [self.label]
        if self.model and self.model not in self.label:
            parts.append(self.model)
        if self.serial:
            parts.append(f"SN {self.serial}")
        parts.append("RX/TX" if self.tx else "RX")
        return " · ".join(parts)
