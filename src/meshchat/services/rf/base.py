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
class MeasuredLimit:
    """What a transport was measured doing, as opposed to what it can be configured for.

    Recorded as data rather than left in prose so the orchestrator budgets from the same
    numbers the audit reports — and tagged with the conditions, so nobody mistakes them
    for a datasheet.
    """

    lossless_rate_hz: float
    sustained_mb_per_s: float
    reliable: bool
    note: str = ""


#: Measured on this bench, 2026-09-27, by injecting a BIST tone and counting phase
#: discontinuities — see ``docs/rf-observatory/PHASE-0-AUDIT.md``. The point of holding
#: both a rate and a reliability flag is that they disagree: raw ``usb:`` is the fastest
#: of the three in its one completed run and is also the one that dropped the board off
#: the bus twice, so the fastest transport is not the one to choose.
MEASURED_LIMITS: dict[str, MeasuredLimit] = {
    ETHERNET: MeasuredLimit(
        lossless_rate_hz=15_000_000,
        sustained_mb_per_s=49.5,
        reliable=True,
        note="60 s soak at 10 MSPS: 1.004x real time, 39.8 MB/s",
    ),
    USB_GADGET: MeasuredLimit(
        lossless_rate_hz=5_000_000,
        sustained_mb_per_s=19.9,
        reliable=True,
        note="60 s soak at 5 MSPS: 1.007x real time",
    ),
    USB: MeasuredLimit(
        lossless_rate_hz=5_000_000,
        sustained_mb_per_s=30.3,
        reliable=False,
        note="one completed run; wedged the board and dropped it off the bus twice",
    ),
}


@dataclass(frozen=True)
class RfCapabilities:
    """What a receiver can be configured to do.

    Deliberately *not* the same thing as what it was measured doing: those live in
    :data:`MEASURED_LIMITS` and in the ``tested_`` fields, because the gap between the two
    is the single most expensive misunderstanding in this hardware. This board advertises
    61.44 MSPS and streams 15; the same vendor's docs concede the general case — *"What you
    will actually get is set by the link to your host, not by the board."*
    """

    min_rate_hz: float | None = None
    max_rate_hz: float | None = None
    rate_step_hz: float | None = None
    min_gain_db: float | None = None
    max_gain_db: float | None = None
    gain_step_db: float | None = None
    max_bandwidth_hz: float | None = None
    #: Significant bits per I or Q sample, and the bytes each one occupies on the wire.
    #: 12-in-16 is why one complex sample costs 4 bytes and not 3, which every throughput
    #: figure in the audit divides by.
    bits: int | None = None
    bytes_per_complex_sample: int | None = None
    notes: tuple[str, ...] = ()


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
    #: Filled by a probe, never by discovery: listing a device must not open it, because
    #: opening takes seconds and fails while something else holds it.
    capabilities: RfCapabilities | None = None

    def measured_limit(self) -> MeasuredLimit | None:
        """What the best transport to this device was measured sustaining."""
        transport = self.streaming_transport()
        return MEASURED_LIMITS.get(transport.kind) if transport is not None else None

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
