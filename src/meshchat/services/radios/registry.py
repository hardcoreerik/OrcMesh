"""Which radios are attached, worked out without opening anything.

The rule this module is built around: **enumeration never opens a port.** Two reasons,
one measured and one emphatic.

Measured: opening a radio takes 13.6 s (COM16, on a bench where another radio was
already connected). Listing what is attached must not cost that, and must not depend on
each candidate answering.

Emphatic: this machine has Espressif ``303A:1001`` USB devices attached that are **not**
radios — one of them never answers the Meshtastic handshake at all. A registry that
identified devices by connecting to them would hang for its full timeout on every
refresh, on a device the user asked to be left alone. So identification by talking to a
radio is a separate, explicit act; this module only ever reports what the operating
system and the radio's own advertisements already said.

Serial numbers on these boards are their MAC addresses, which is what makes the grouping
in `base.mac_family` possible without contacting anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import base

#: USB vendor/product pairs seen on Meshtastic ESP32 boards. A *hint*, never an
#: identification: `303A:1001` is Espressif's own USB-serial/JTAG block, which any ESP32
#: dev board exposes, so matching it means "might be a radio, worth offering to the user"
#: and not "is a radio". Confirmed on this bench with the Heltec V4 (COM24) and the
#: T-Beam S3 Core (COM16).
MESHTASTIC_USB_IDS: frozenset[tuple[int, int]] = frozenset({(0x303A, 0x1001)})

#: Serial ports that are links to something else and can never be a radio on this bench:
#: Windows exposes every paired Bluetooth serial profile as a COM port.
_NOT_A_PORT_PREFIX = ("BTHENUM",)


@dataclass(frozen=True)
class PortSummary:
    """A serial port as the operating system describes it. No port is opened to build one."""

    device: str
    description: str = ""
    hwid: str = ""
    serial_number: str | None = None
    vid: int | None = None
    pid: int | None = None

    @property
    def is_bluetooth_link(self) -> bool:
        return self.hwid.upper().startswith(_NOT_A_PORT_PREFIX)

    @property
    def looks_like_a_radio(self) -> bool:
        if self.is_bluetooth_link:
            return False
        if self.vid is not None and self.pid is not None:
            return (self.vid, self.pid) in MESHTASTIC_USB_IDS
        # No VID/PID means the port was described some other way, so there is nothing to
        # match on. Offered rather than hidden: a radio behind an unusual USB bridge is
        # still a radio, and hiding it would be a silent failure.
        return True


@dataclass(frozen=True)
class BleAdvertisement:
    """A Bluetooth advertisement, already received. Scanning is the caller's business."""

    address: str
    name: str = ""
    rssi: int | None = None


@dataclass
class Candidate:
    """One radio, and every way of reaching it that has not yet been ruled out."""

    key: str
    transports: list[base.RadioTransport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def preferred(self) -> base.RadioTransport | None:
        usable = [t for t in self.transports if t.rank != base.UNRANKED]
        if not usable:
            return None
        return min(usable, key=lambda t: t.rank)

    def describe(self) -> str:
        if not self.transports:
            return self.key
        ordered = sorted(self.transports, key=lambda t: t.rank)
        return f"{self.key} via " + ", ".join(t.describe() for t in ordered)


def candidates(
    ports: list[PortSummary] | None = None,
    ads: list[BleAdvertisement] | None = None,
) -> list[Candidate]:
    """Group what is attached into one entry per radio, best transport first.

    The grouping is what makes this more than a port list: a board that reports its MAC
    on serial *and* advertises over Bluetooth appears once, with both doors, because
    those are one radio. Serial leads the transport list for the reason `base.PREFERENCE`
    gives.
    """
    found: dict[str, Candidate] = {}

    def entry(key: str) -> Candidate:
        if key not in found:
            found[key] = Candidate(key=key)
        return found[key]

    for port in ports or []:
        if not port.looks_like_a_radio:
            continue
        serial = (port.serial_number or "").strip()
        key = base.candidate_key(address=serial or port.device)
        candidate = entry(key)
        candidate.transports.append(
            base.RadioTransport(
                kind=base.SERIAL,
                address=port.device,
                label=f"{port.device} ({port.description})" if port.description else port.device,
            ),
        )
        if not base.look_like_mac(serial):
            candidate.notes.append(
                "the serial number is not a hardware address, so this entry can only be "
                "matched to another transport by asking the radio",
            )

    for ad in ads or []:
        # No special case for matching a Bluetooth address to a serial one: candidate_key
        # puts both through mac_family first, so a board's serial MAC and its BLE MAC+1
        # produce the same key and land in the same entry without anything looking for
        # them. That is the whole reason the normalisation lives in base.
        candidate = entry(base.candidate_key(address=ad.address))
        label = f"{ad.address} (Bluetooth"
        label += f", {ad.name}" if ad.name else ""
        label += f", {ad.rssi} dBm)" if ad.rssi is not None else ")"
        candidate.transports.append(
            base.RadioTransport(kind=base.BLE, address=ad.address, label=label),
        )

    return sorted(found.values(), key=lambda c: (c.preferred() is None, c.key))


def ports_from_comports(comports) -> list[PortSummary]:
    """Adapt `serial.tools.list_ports` objects without importing the library here.

    Duck-typed on purpose: the registry stays importable — and testable — on a machine
    with no serial stack, which is also how it stays honest about never opening a port.
    """
    summaries = []
    for port in comports:
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        summaries.append(
            PortSummary(
                device=str(getattr(port, "device", "")),
                description=str(getattr(port, "description", "") or ""),
                hwid=str(getattr(port, "hwid", "") or ""),
                serial_number=getattr(port, "serial_number", None),
                vid=vid,
                pid=pid,
            ),
        )
    return summaries
