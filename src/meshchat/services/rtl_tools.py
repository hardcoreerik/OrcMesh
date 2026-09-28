"""Locating and driving the native RTL-SDR command-line tools.

OrcMesh never links or vendors an SDR library. It drives the tools that come
with the rtl-sdr driver as child processes, for the same reasons the OrcMaps
integration does: the native build already on the machine is the one that
knows its hardware, and a driver that misbehaves can only take down a child
process, never the app.

That is also *why* this is not pyrtlsdr. A Python binding carries a librtlsdr
of its own, and the DLL inside the pyrtlsdr wheel is normally older than an
RTL-SDR Blog V4 needs — the V4's R828D tuner reports itself as an R820T, and an
old driver then mishandles the V4's band switching. The tools from the
rtl-sdr-blog release are built for it and are already on PATH on any machine
where this works at all.

Verified against the real tools on a Blog V4:

* ``rtl_sdr`` streams interleaved unsigned 8-bit I/Q on **stdout**; its banner,
  tuning confirmation and any error go to **stderr**. The split is what makes
  it safe to parse: IQ on one stream, diagnostics on the other.
* ``rtl_power`` writes a stitched sweep to stdout as CSV — ``date, time,
  low_hz, high_hz, step_hz, nsamples, dB...`` — one row per sub-band per
  interval, so a whole region band arrives already stitched.
* ``rtl_test`` opens the device and reports it, which is the only cheap way to
  tell "no dongle" apart from "dongle is busy".
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)

#: Tools OrcMesh knows how to drive. `rtl_sdr` is the only hard requirement;
#: the other two add capability but their absence is not fatal.
TOOL_NAMES = ("rtl_sdr", "rtl_power", "rtl_test")

#: An RTL-SDR can only be open in one process at a time, so a machine that is
#: running SDR#, Gqrx or SDR++ will make every capture fail. Every driver says
#: it differently, so all the phrasings are listed rather than guessing one.
BUSY_MARKERS = (
    "failed to open rtlsdr device",
    "usb_open error",
    "device is busy",
    "resource busy",
    "access is denied",
)


def tools_directory() -> Path:
    """Where OrcMesh looks for tools that are not on PATH.

    A per-user directory rather than the system PATH, for two reasons. Installing a
    decoder should not mean editing a machine-wide environment variable, and an
    uninstall should be "delete this folder". And these are other projects' GPL
    binaries, not something OrcMesh ships: pointing at one has to be an explicit act
    rather than something that happens by being present.
    """
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "OrcMesh" / "tools"


def find_tool(name: str) -> Path | None:
    """Absolute path to one of the native tools, or None if it is not installed.

    PATH first, so a system install always wins and this can never shadow it, then
    the per-user tools directory.
    """
    found = shutil.which(name)
    if found:
        return Path(found)
    directory = tools_directory()
    # The .exe suffix is spelled out because shutil.which is not the one looking here.
    for candidate in (directory / name, directory / f"{name}.exe"):
        if candidate.is_file():
            return candidate
    return None


def tool_paths() -> dict[str, Path]:
    """Every native tool that could be found, keyed by name."""
    found: dict[str, Path] = {}
    for name in TOOL_NAMES:
        path = find_tool(name)
        if path is not None:
            found[name] = path
    return found


def creation_flags() -> int:
    """CREATE_NO_WINDOW on Windows, 0 elsewhere.

    Release builds run under pythonw.exe, where any child process without this
    would flash a console window on screen for as long as it runs.
    """
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def tools_available() -> tuple[bool, str]:
    """Return (available, reason), naming exactly what is missing.

    Deliberately does not open the device. That takes seconds, and a failure
    while another program holds the dongle is indistinguishable from a failure
    because there is no dongle — the real message is only available when a
    capture actually starts, so the device is checked there instead.
    """
    paths = tool_paths()
    if "rtl_sdr" not in paths:
        return False, (
            "The native RTL-SDR tools were not found on PATH (rtl_sdr is required)."
            "\n\nOn Windows the rtl-sdr-blog release installs rtl_sdr.exe and "
            "binds the WinUSB driver for the dongle. Install that, then reopen "
            "OrcMesh."
        )
    extras = [name for name in ("rtl_power", "rtl_test") if name in paths]
    summary = f"rtl_sdr found at {paths['rtl_sdr'].parent}"
    if extras:
        summary += f" (with {', '.join(extras)})"
    else:
        summary += " (rtl_power and rtl_test are missing: band scanning and the device check need them)"
    return True, summary


def explain_failure(text: str, tool: str = "rtl_sdr") -> str:
    """Turn a native tool's stderr into something a user can act on.

    `tool` names whichever tool failed, so a scan failure does not blame
    rtl_sdr and send the user looking at the wrong thing.
    """
    stripped = text.strip()
    lowered = stripped.lower()
    if any(marker in lowered for marker in BUSY_MARKERS):
        return (
            "The dongle is already in use by another program.\n\n"
            "An RTL-SDR can only be open in one process at a time. Close SDR#, "
            "Gqrx, SDR++ or any running rtl_* tool, then start again."
            + (f"\n\n{tool} said: {stripped}" if stripped else "")
        )
    if "no supported devices" in lowered or "no devices found" in lowered:
        return f"No RTL-SDR dongle was found. Is it plugged in?\n\n{stripped}"
    return stripped or f"{tool} stopped without reporting a reason."


_DEVICE_COUNT = re.compile(r"found\s+(\d+)\s+device", re.IGNORECASE)

#: One ``<index>: <manufacturer>, <product>, SN: <serial>`` line of the device table.
_DEVICE_LINE = re.compile(r"^\s*(\d+):\s*(?P<body>.*?)\s*$")

#: Anything that is not printable ASCII, removed from a device's identity fields.
#:
#: A dongle that cannot be opened does not report no name — it reports whatever was in
#: the read buffer, and the driver still lists it. Verbatim from this machine with
#: device 0 held by OrcMesh itself:
#:
#:     0:  \x01, , SN: \xff
#:
#: which reaches Python as control characters plus a stray 0xFF, and would otherwise
#: put a device called "ÿ" in the selector. Nothing legitimate is lost: a programmed
#: EEPROM holds an ASCII product name.
_NON_PRINTABLE = re.compile(r"[^\x20-\x7e]")


def _clean_field(text: str) -> str:
    return _NON_PRINTABLE.sub("", text).strip()


@dataclass(frozen=True)
class SdrDevice:
    """One dongle as the driver lists it.

    Keyed on INDEX, not on serial, and that is forced by the hardware rather than
    chosen: the two dongles on this machine both report ``SN: 00000001``, the same
    value the rtl-sdr blog EEPROM ships with, so a serial-based selector would have
    nothing to tell them apart with.
    """

    index: int
    manufacturer: str = ""
    product: str = ""
    serial: str = ""

    @property
    def label(self) -> str:
        """What a dongle selector shows: index first, because that is the identity."""
        name = self.product or self.manufacturer or "RTL-SDR"
        return f"{self.index} · {name}"

    def describe(self) -> str:
        """The full name, for a tooltip or a status line."""
        text = " ".join(part for part in (self.manufacturer, self.product) if part)
        if not text:
            # A blank EEPROM and a dongle that could not be opened are indistinguishable
            # in the listing — an unopened device returns unread buffer bytes, which the
            # cleaning above reduces to nothing. Both mean "no name", so say that
            # rather than inventing one, and name the likely cause.
            return "RTL-SDR — name unreadable (blank EEPROM, or the device is in use)"
        if self.serial:
            text += f" (SN {self.serial})"
        return text


def parse_device_list(text: str) -> list[SdrDevice]:
    """The device table out of ``rtl_test``'s output.

    The real shape, verbatim from the blog driver with two dongles attached:

        Found 2 device(s):
          0:  RTLSDRBlog, Blog V4, SN: 00000001
          1:  RTLSDRBlog, Blog V4L, SN: 00000001

    Two things worth knowing about it. Both dongles can carry the SAME serial, which
    is why everything downstream is keyed on the index. And the tuner type is *not*
    here — learning that means opening the device, which is the one thing a listing
    must not do, because opening takes seconds and fails while anything else holds it.
    """
    devices: list[SdrDevice] = []
    in_table = False
    for line in text.splitlines():
        if _DEVICE_COUNT.search(line):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.strip():
            continue
        match = _DEVICE_LINE.match(line)
        if match is None:
            break  # whatever follows the table is not a device

        fields = [_clean_field(field) for field in match.group("body").split(",")]
        serial = ""
        if fields and fields[-1].upper().startswith("SN:"):
            serial = _clean_field(fields[-1].split(":", 1)[1])
            fields = fields[:-1]
        # A clone with a blank EEPROM lists as ", , SN:" — empty is a valid answer and
        # is kept as one, rather than being read as a device with the name "SN:".
        manufacturer = fields[0] if fields else ""
        product = ", ".join(fields[1:]) if len(fields) > 1 else ""
        devices.append(
            SdrDevice(
                index=int(match.group(1)),
                manufacturer=manufacturer,
                product=product,
                serial=serial,
            )
        )
    return devices


def list_devices(timeout_s: float = 8.0) -> tuple[list[SdrDevice], str]:
    """Every dongle the driver can see, plus a reason when it cannot look.

    ``rtl_test -t`` lists them all without OrcMesh opening any, which is what makes
    this usable as a refresh in a selector: it also succeeds while another program is
    capturing, where opening one would fail.
    """
    tool = find_tool("rtl_test")
    if tool is None:
        return [], "rtl_test was not found, so the dongles cannot be listed."

    try:
        completed = subprocess.run(
            [str(tool), "-t"], capture_output=True, text=True, timeout=timeout_s,
            errors="replace", creationflags=creation_flags(),
        )
    except subprocess.TimeoutExpired:
        return [], (
            "rtl_test did not finish in time, which usually means the driver is "
            "stuck. Unplug and replug the dongle."
        )
    except OSError as exc:
        return [], f"Could not run rtl_test: {exc}"

    # rtl_test reports on both streams depending on the build, so both are read.
    text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    devices = parse_device_list(text)
    if not devices:
        return [], explain_failure(text)
    return devices, f"{len(devices)} RTL-SDR dongle(s) found"


def probe_device(timeout_s: float = 8.0) -> tuple[int, str]:
    """Open the dongle once via ``rtl_test`` and report what it found.

    Returns (device_count, message). Bounded by a hard timeout because this
    runs a real tool against real hardware: a wedged driver must not wedge
    OrcMesh. This is the only reliable way to distinguish an absent dongle
    from one another program is holding, since both look identical from
    OrcMesh's side until something tries to open it.
    """
    tool = find_tool("rtl_test")
    if tool is None:
        return 0, "rtl_test was not found, so the dongle cannot be checked."

    try:
        completed = subprocess.run(
            [str(tool), "-t"], capture_output=True, text=True, timeout=timeout_s,
            errors="replace", creationflags=creation_flags(),
        )
    except subprocess.TimeoutExpired:
        return 0, (
            "rtl_test did not finish in time, which usually means the driver is "
            "stuck. Unplug and replug the dongle."
        )
    except OSError as exc:
        return 0, f"Could not run rtl_test: {exc}"

    # rtl_test reports on both streams depending on the build, so both are read.
    text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    match = _DEVICE_COUNT.search(text)
    count = int(match.group(1)) if match else 0

    if count == 0:
        return 0, explain_failure(text)

    # The device list is indented, one "<index>: <name>" per line — line 0 is
    # the one this probe opened.
    name = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("0:"):
            name = stripped[2:].strip()
            break
    detail = f"{count} RTL-SDR dongle(s) found" + (f": {name}" if name else "")
    return count, detail


class StderrCollector:
    """Keep the tail of a child process's stderr without blocking on it.

    Needed for two reasons: a tool that writes more than the pipe buffer holds
    would block mid-run if nothing drained it, and the last line it wrote is
    the only description of what went wrong when it dies. Owns its own thread
    because the reading side is blocked on stdout and cannot service it.

    This is for diagnostics only, never for deciding success — a clean exit
    still writes noise here (rtl_sdr signs off with a demod register error as
    it closes the device, on a successful run).
    """

    def __init__(self, stream: IO[bytes] | None, max_lines: int = 40) -> None:
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        if stream is not None:
            self._thread = threading.Thread(
                target=self._drain, args=(stream,), daemon=True,
            )
            self._thread.start()

    def _drain(self, stream: IO[bytes]) -> None:
        try:
            for raw in stream:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    with self._lock:
                        self._lines.append(line)
        except (OSError, ValueError) as exc:
            # Expected when the pipe is closed out from under us on stop.
            log.debug("stderr drain ended: %s", exc)

    def tail(self) -> str:
        """The last line seen — the one that explains an exit."""
        with self._lock:
            return self._lines[-1] if self._lines else ""

    def text(self) -> str:
        """Everything kept, for a diagnostics view."""
        with self._lock:
            return "\n".join(self._lines)

    def join(self, timeout: float = 1.0) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)


#: Only one process can hold an RTL-SDR, and that includes OrcMesh's own features:
#: the Spectrum view and the SIGINT view both want one. Ownership is therefore tracked
#: per DEVICE rather than per process — with two dongles attached, one view can hold
#: device 0 while another holds device 1, which is the whole point of supporting more
#: than one. What must still never happen is two features opening the SAME dongle, and
#: that is what this refuses.
_sdr_owners: dict[int, tuple[str, str]] = {}
_sdr_lock = threading.Lock()


def acquire_sdr(owner: str, label: str, device: int = 0) -> tuple[bool, str]:
    """Claim one dongle for `owner`, naming it as `label` in any complaint.

    Re-acquiring as the same owner succeeds, so a restart inside one feature does
    not have to release first.
    """
    with _sdr_lock:
        held = _sdr_owners.get(device)
        if held is not None and held[0] != owner:
            return False, (
                f"Dongle {device} is already in use by {held[1]}.\n\n"
                "Each RTL-SDR can only be open in one place at a time — stop that "
                "capture first, or point this view at the other dongle."
            )
        _sdr_owners[device] = (owner, label)
        return True, ""


def release_sdr(owner: str, device: int = 0) -> None:
    """Give the dongle back. Releasing someone else's lease does nothing."""
    with _sdr_lock:
        held = _sdr_owners.get(device)
        if held is not None and held[0] == owner:
            del _sdr_owners[device]


def sdr_owner(device: int = 0) -> str:
    """Name of whatever is currently holding that dongle, or an empty string."""
    with _sdr_lock:
        held = _sdr_owners.get(device)
        return held[1] if held is not None else ""


#: Children this process has started and not yet seen exit. The lease above records who
#: *intends* to hold a dongle; this records which processes *actually* exist. The two can
#: disagree in one way that matters: OrcMesh goes away without running its stop path, the
#: child keeps the device open, and the next launch reports a busy dongle with no capture
#: running and nothing for the user to stop. Reaping at exit is what closes that gap.
_children: dict[int, tuple[str, subprocess.Popen[bytes]]] = {}
_children_lock = threading.Lock()
_atexit_hooked = False


def _hook_atexit() -> None:
    global _atexit_hooked
    if _atexit_hooked:
        return
    atexit.register(terminate_children)
    _atexit_hooked = True


def track_child(process: subprocess.Popen[bytes], label: str) -> None:
    """Remember a child so it can be reaped later. Idempotent per process."""
    with _children_lock:
        _children[process.pid] = (label, process)
    _hook_atexit()


def untrack_child(process: subprocess.Popen[bytes] | None) -> None:
    """Forget a child that has been waited on. Safe to call with None."""
    if process is None:
        return
    with _children_lock:
        _children.pop(process.pid, None)


def tracked_children() -> list[str]:
    """Labels of the children still believed to be running, for a diagnostics view."""
    with _children_lock:
        return [label for label, _ in _children.values()]


def terminate_children(timeout_s: float = 2.0) -> int:
    """Terminate every tracked child that has not exited; return how many were stopped.

    Registered with `atexit` the first time a child is tracked, so a clean exit and an
    unhandled exception both release the hardware rather than leaving a dongle held by a
    process nobody can see. It must therefore not raise and must not wait long: a wedged
    driver that ignores `terminate()` gets killed, and anything still unresponsive after
    that is left to the OS rather than holding the app open on the way out.

    This does **not** cover a hard kill of OrcMesh itself (Task Manager, a power loss).
    A Windows Job Object would, and is the documented upgrade; it needs the Win32 API and
    a per-child assignment at spawn, which is future work rather than a claim.
    """
    with _children_lock:
        children = list(_children.values())
        _children.clear()

    stopped = 0
    for label, process in children:
        if process.poll() is not None:
            continue
        try:
            process.terminate()
            process.wait(timeout=timeout_s)
            stopped += 1
            log.info("Reaped %s (pid %d) on exit", label, process.pid)
        except subprocess.TimeoutExpired:
            log.warning("%s (pid %d) ignored terminate(); killing it", label, process.pid)
            try:
                process.kill()
                process.wait(timeout=timeout_s)
                stopped += 1
            except (OSError, subprocess.TimeoutExpired):
                log.error("%s (pid %d) could not be killed", label, process.pid)
        except OSError:
            # Already gone, or not ours to signal.
            log.debug("Could not terminate %s (pid %d)", label, process.pid, exc_info=True)
    return stopped


def spawn(
    args: Sequence[str | Path], *, label: str, **kwargs: object,
) -> subprocess.Popen[bytes]:
    """Start a tool and remember the child.

    Every capture and every survey goes through here rather than calling `Popen`
    directly: an untracked child is a child that can outlive the app still holding the
    dongle, and that is not a mistake worth leaving available to the next feature that
    needs a device.
    """
    process = subprocess.Popen(  # type: ignore[call-overload]
        [str(part) for part in args], **kwargs,
    )
    track_child(process, label)
    return process


#: The tuner's own gain table, as ``rtl_test -t`` reports it on the R828D/R820T
#: in the Blog V4 this was built against. Offering these rather than an arbitrary
#: number matters: the tuner snaps to its nearest step, so a value off the list
#: silently becomes a different gain than the one asked for, and the user has no
#: way to tell which one they got.
R820T_GAIN_STEPS: tuple[float, ...] = (
    0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
    16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4,
    37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6,
)

#: Where to start a capture or a survey. Deliberately not 0, which rtl_sdr reads
#: as automatic gain — that resolves to near-maximum, the worst case for
#: headroom: measured on this dongle, auto put the noise floor at +17 dB against
#: -9 dB at 15.7 dB of gain, leaving strong signals nowhere to go.
DEFAULT_GAIN_DB = 16.0
