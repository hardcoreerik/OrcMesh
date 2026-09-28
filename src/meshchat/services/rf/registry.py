"""Which radios are attached, as one entry per physical device.

The problem this exists to solve was measured, not imagined. One PlutoSDR clone answers to
three libiio contexts at once — captured verbatim from this bench::

    $ iio_info -s
    Unable to create Local IIO context : Function not implemented (40)
    Available contexts:
            0: 192.168.1.50 (FISH Ball PlutoSDR Rev.A (Z7020-AD9361)), serial=b0d85d89... [ip:pluto.local]
            1: 192.168.2.1 (FISH Ball PlutoSDR Rev.A (Z7020-AD9361)), serial=b0d85d89... [ip:pluto.local]
            2: 0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial=b0d85d89... [usb:1.66.5]

Three contexts, one board, one serial. A registry keyed on URI would show three receivers
and one radio — and would then let two of them fight over a single tuner. So the serial is
the identity and the contexts become transports.

Two further details from that same measurement are encoded here rather than left to a
caller to remember:

* **The USB context's model string is wrong.** Over USB the clone's descriptors say
  "Analog Devices Inc. PlutoSDR (ADALM-PLUTO)" while the board reports the FISH Ball model.
  The vendor documentation attributes that to reference-firmware strings the clone
  inherited, and it matters because a UI repeating the USB claim would mislabel the
  hardware — so the ``ip:`` model wins and the disagreement is recorded, not hidden.
* **RTL dongles cannot be identified by serial.** This machine's two both report
  ``SN: 00000001``, so the bus index — which is what the driver itself selects by — is the
  key, and each one carries a note saying its identity is positional.

RTL devices deliberately carry no transports: they are addressed by index through the
native tools rather than by URI, so ``key`` is their address.
"""
from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from .. import rtl_tools
from .base import PREFERENCE, UNRANKED, RfDeviceInfo, RfTransport, transport_kind

log = logging.getLogger(__name__)

#: One line of ``iio_info -s``. Everything else in that output is noise for our purposes:
#: the Windows local-backend warning printed before the list, or "No IIO context found."
#: in place of one.
_CONTEXT_LINE = re.compile(
    r"^\s*(?P<index>\d+):\s*(?P<address>\S+)\s+"
    # `.+` rather than `[^)]*`, because the model string carries parentheses of its own:
    # "FISH Ball PlutoSDR Rev.A (Z7020-AD9361)" stops a class-excluding-`)` at the inner
    # one and then fails to find the ", serial=" that follows. Greedy, backtracking to the
    # last ")" before the serial.
    # `\S*` rather than `\S+`, because a context may report an empty serial, and dropping
    # such a board out of the listing is worse than listing it keyed on its address.
    r"\((?P<model>.+)\)\s*,\s*serial=(?P<serial>\S*)\s*"
    r"\[(?P<uri>[^\]]+)\]\s*$"
)

#: Long enough for a USB scan, short enough that a wedged driver cannot stall a refresh.
SCAN_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class IioContext:
    """One line of ``iio_info -s``: a URI, and what answers there."""

    index: int
    address: str
    model: str
    serial: str
    uri: str

    @property
    def kind(self) -> str:
        # Classified from the handle rather than the reported URI: both of this board's
        # ip: contexts report the same discovery name, so the name cannot distinguish the
        # Ethernet port from the gadget and would rank them identically.
        return transport_kind(self.transport_uri)

    @property
    def transport_uri(self) -> str:
        """The handle to actually use, which is not always the one libiio reports.

        For an ``ip:`` context the bracketed URI is the *discovery* name — ``ip:pluto.local``
        — and the board's two interfaces both answer to it, so in the verbatim listing
        above contexts 0 and 1 carry **the same URI**. It therefore cannot tell the
        Ethernet port from the USB gadget, which are 3x apart in throughput. The address
        field can, and libiio accepts an address directly.

        A ``usb:`` context is the reverse: its URI (``usb:1.66.5``) is the usable one,
        while its address (``0456:b673``) is a VID:PID that no URI accepts.
        """
        if self.uri.startswith("ip:"):
            return f"ip:{self.address}"
        return self.uri


def parse_contexts(text: str) -> list[IioContext]:
    """Every context line in ``iio_info -s`` output, skipping everything else.

    Unmatched lines are skipped rather than treated as an error: the tool prints a
    "Unable to create Local IIO context" warning on Windows before the list, and prints
    "No IIO context found." when nothing is attached. Neither should cost a parse.
    """
    contexts: list[IioContext] = []
    for line in text.splitlines():
        match = _CONTEXT_LINE.match(line)
        if match is None:
            continue
        contexts.append(
            IioContext(
                index=int(match.group("index")),
                address=match.group("address").strip(),
                model=match.group("model").strip(),
                serial=match.group("serial").strip(),
                uri=match.group("uri").strip(),
            )
        )
    return contexts


def pluto_devices(contexts: Sequence[IioContext]) -> list[RfDeviceInfo]:
    """Group IIO contexts into one device per serial.

    The grouping *is* the feature: three contexts over one serial is one radio.
    """
    grouped: dict[str, list[IioContext]] = {}
    for context in contexts:
        # With no serial there is nothing to group on, so the address stands in. A device
        # listed twice is a smaller error than two unrelated boards silently merged.
        key = context.serial or f"noserial:{context.address}"
        grouped.setdefault(key, []).append(context)

    devices: list[RfDeviceInfo] = []
    for serial, group in grouped.items():
        ordered = sorted(group, key=lambda context: PREFERENCE.get(context.kind, UNRANKED))
        transports = tuple(RfTransport(uri=context.transport_uri) for context in ordered)

        reported = next((c.model for c in ordered if c.kind != "usb" and c.model), "")
        claimed = sorted({c.model for c in ordered if c.kind == "usb" and c.model})
        model = reported or (claimed[0] if claimed else "")

        notes: list[str] = []
        if claimed and "Analog Devices" in claimed[0] and "Analog Devices" not in reported:
            notes.append(
                f"the USB descriptor claims {claimed[0]!r} while the board reports "
                f"{reported!r}; this clone inherits the reference firmware's strings, so "
                "the USB name is not used"
            )
        if len(transports) > 1:
            kinds = ", ".join(sorted({t.kind for t in transports}))
            notes.append(
                f"reachable over {len(transports)} transports ({kinds}) — one radio, so "
                "only one of them can hold the tuner at a time"
            )

        devices.append(
            RfDeviceInfo(
                key=f"pluto:{serial}",
                backend="pluto_iio",
                label=model or "PlutoSDR",
                model=model,
                serial=serial,
                # Measured, not assumed: cf-ad9361-lpc and the TX DMA each expose four
                # channels, i.e. two complex streams each way.
                tx=True,
                transports=transports,
                notes=tuple(notes),
            )
        )
    return devices


def rtl_devices(dongles: Sequence[rtl_tools.SdrDevice]) -> list[RfDeviceInfo]:
    """The dongles, keyed on bus index because the hardware offers nothing better."""
    serials = [dongle.serial.strip() for dongle in dongles]
    indistinguishable = {serial for serial in serials if not serial or serials.count(serial) > 1}

    devices: list[RfDeviceInfo] = []
    for dongle in dongles:
        notes: list[str] = []
        if dongle.serial.strip() in indistinguishable:
            notes.append(
                "keyed on bus index: this dongle reports no distinguishing serial "
                f"(it lists as {dongle.serial!r}), so its identity can change if devices "
                "are replugged"
            )
        devices.append(
            RfDeviceInfo(
                key=f"rtl:{dongle.index}",
                backend="rtl_sdr",
                label=dongle.label,
                model=dongle.describe(),
                serial=dongle.serial,
                tx=False,
                transports=(),
                notes=tuple(notes),
            )
        )
    return devices


def discover(timeout_s: float = SCAN_TIMEOUT_S) -> tuple[list[RfDeviceInfo], tuple[str, ...]]:
    """Every radio reachable right now, plus anything that stopped us looking.

    Listing only. Nothing here opens a device or claims an exclusive resource, which is
    what makes it safe to call while a capture is already running.
    """
    devices: list[RfDeviceInfo] = []
    problems: list[str] = []

    dongles, rtl_message = rtl_tools.list_devices(timeout_s=timeout_s)
    devices.extend(rtl_devices(dongles))
    if not dongles:
        problems.append(rtl_message)

    scan = _scan_iio_contexts(timeout_s)
    if scan is None:
        problems.append("iio_info was not found, so no PlutoSDR can be listed.")
    else:
        devices.extend(pluto_devices(parse_contexts(scan)))

    return devices, tuple(problems)


def _scan_iio_contexts(timeout_s: float) -> str | None:
    """``iio_info -s`` output, or None when the tool is not installed.

    The tool lookup and the process flags come from :mod:`rtl_tools`: neither is
    RTL-specific, and a second copy of the same subprocess plumbing would be a second
    thing to get wrong.
    """
    tool = rtl_tools.find_tool("iio_info")
    if tool is None:
        return None
    try:
        completed = subprocess.run(
            [str(tool), "-s"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            errors="replace",
            creationflags=rtl_tools.creation_flags(),
        )
    except subprocess.TimeoutExpired:
        log.warning("iio_info -s did not finish within %.1f s", timeout_s)
        return ""
    except OSError:
        log.exception("Could not run iio_info")
        return ""
    # This tool reports on both streams depending on the build, exactly as rtl_test does.
    return f"{completed.stdout or ''}\n{completed.stderr or ''}"
