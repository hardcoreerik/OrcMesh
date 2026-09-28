"""Probing what a receiver can be configured to do.

Separate from the registry on purpose. Discovery **lists without opening** — the same
posture ``rtl_test -t`` takes, because opening a receiver costs seconds and fails while
something else holds it. Probing is therefore an explicit act, and this is where it lives.

Every function that reads a tool's text is pure here, so the parsing is tested against
verbatim output from this bench rather than a guess at a format. The one that talks to
hardware takes a runner, so it can be tested without a radio attached.
"""
from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence

from .. import rtl_tools
from .base import RfCapabilities

log = logging.getLogger(__name__)

#: The chip this layer currently targets. Kept as a parameter rather than hard-coded into
#: the calls, because "ad9361-phy" is a property of the AD9361 family and not of libiio.
DEFAULT_PHY_DEVICE = "ad9361-phy"
DEFAULT_PHY_CHANNEL = "voltage0"

#: The receive DMA, and the device the sample format has to be read from. Not merely the
#: first ``format:`` in a listing: ``iio_info`` lists the **transmit** DMA
#: (``cf-ad9361-dds-core-lpc``) before it, and the transmit path is ``le:S16/16`` while the
#: receiver is ``le:S12/16``. Reading the first format therefore reports the wrong
#: resolution — caught by probing this board, where the live answer came back as 16 bits.
DEFAULT_RX_DEVICE = "cf-ad9361-lpc"

#: Rates this firmware silently refuses. Setting ``sampling_frequency`` below roughly
#: 3 MSPS left the device at its previous value and reported success, confirmed on
#: 2026-09-27 — so the 2,083,333 Hz the attribute advertises as its minimum is not
#: actually usable. It is the clearest case on this bench of why a claimed capability and
#: a working one have to be recorded as different things.
UNUSABLE_BELOW_HZ = 3_000_000

_CHANNEL_FORMAT = re.compile(r"format:\s*le:S(?P<bits>\d+)/(?P<container>\d+)")


def parse_range(text: str) -> tuple[float, float, float] | None:
    """``(low, step, high)`` from libiio's range syntax, or None.

    Two shapes occur and both are handled, because both turn up in practice: the bare
    ``[2083333 1 61440000]`` returned when a named attribute is queried, and the verbose
    ``... attr 'x', value '[2083333 1 61440000]'`` form returned when a whole channel is
    listed. A single number is also valid, for a range with no step, and comes back with
    a zero step rather than as a failure.
    """
    if "value" in text:
        text = text.split("value", 1)[1]
    bracketed = re.search(r"\[([^\]]*)\]", text)
    if bracketed is not None:
        text = bracketed.group(1)
    numbers = [float(token) for token in re.findall(r"-?\d+(?:\.\d+)?", text)]
    if len(numbers) == 1:
        return numbers[0], 0.0, numbers[0]
    if len(numbers) >= 3:
        return numbers[0], numbers[1], numbers[2]
    return None


def parse_sample_format(
    iio_info_text: str, device: str = DEFAULT_RX_DEVICE,
) -> tuple[int, int] | None:
    """``(significant bits, container bits)`` for one device, or None.

    Scoped to a named device rather than taking the first ``format:`` in the listing, and
    the reason is a real trap: ``iio_info`` lists the transmit DMA before the receive one,
    the transmit path is ``le:S16/16`` and the receiver is ``le:S12/16``. Taking the first
    format reports 16 bits for a 12-bit receiver, which reads as plausible and is wrong.

    The container width is what costs bytes: 12 bits in a 16-bit container is 2 bytes per
    I or Q, so one complex sample is 4 bytes and not the 3 the resolution suggests.

    If the named device is absent the answer is None rather than a fallback to some other
    device's format, because a plausible wrong width is worse than an admitted unknown.
    """
    lines = iio_info_text.splitlines()
    for index, line in enumerate(lines):
        if device in line and line.lstrip().startswith("iio:device"):
            for following in lines[index:]:
                match = _CHANNEL_FORMAT.search(following)
                if match is not None:
                    return int(match.group("bits")), int(match.group("container"))
            return None
    return None


def read_first_line(text: str) -> str | None:
    """The first non-empty line of a tool's output.

    Which line matters, and the reason is a mistake that cost real time here: asked for a
    named attribute, ``iio_attr`` prints bare values, **one line per matching channel,
    input first then output**. Taking the last value reports the transmit side, and makes
    a successful receive-side setting look like it never happened.
    """
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def _run_tool(args: Sequence[str], timeout_s: float) -> str:
    """Run one of the libiio command-line tools and return both streams as text."""
    try:
        completed = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout_s, errors="replace",
        )
    except subprocess.TimeoutExpired:
        log.warning("%s did not finish within %.1f s", args[0], timeout_s)
        return ""
    except OSError:
        log.exception("Could not run %s", args[0])
        return ""
    return f"{completed.stdout or ''}\n{completed.stderr or ''}"


def _read(runner: object, args: Sequence[str], timeout_s: float) -> str:
    """One tool call, degrading to empty output rather than raising.

    A probe runs against hardware that can be unplugged or wedged mid-question, and a
    caller asking what a receiver can do should get a partial answer plus a note — not an
    exception thrown from four layers down in a listing refresh.
    """
    if not callable(runner):  # pragma: no cover - defensive, keeps the annotation honest
        return ""
    try:
        return runner(list(args), timeout_s)
    except (OSError, subprocess.SubprocessError):
        log.warning("Probe call failed: %s", " ".join(str(part) for part in args), exc_info=True)
        return ""


def probe_libiio(
    uri: str,
    *,
    device: str = DEFAULT_PHY_DEVICE,
    channel: str = DEFAULT_PHY_CHANNEL,
    timeout_s: float = 8.0,
    run: object = _run_tool,
) -> RfCapabilities | None:
    """Ask a libiio device what it can do, or None when it cannot be reached.

    Pass ``run`` to supply a different runner in tests. Nothing is guessed from the model
    name: a device that answers for its rate but not its gain comes back with the rate and
    an explicit note, rather than with plausible defaults.
    """
    runner = run
    if not callable(runner):
        # A caller-supplied runner is the test seam; one that cannot be called is a
        # programming error, and answering with the real tools instead would hide it.
        return None

    def attribute(attribute_name: str) -> str:
        text = _read(runner, ["iio_attr", "-u", uri, "-c", device, channel, attribute_name], timeout_s)
        return read_first_line(text) or ""

    if rtl_tools.find_tool("iio_attr") is None:
        return None

    notes: list[str] = []

    rate = parse_range(attribute("sampling_frequency_available"))
    if rate is None:
        notes.append("the device did not report a sample-rate range")
    elif rate[0] < UNUSABLE_BELOW_HZ:
        notes.append(
            f"the advertised minimum {rate[0]:.0f} Hz was not usable on this firmware: "
            f"requests below about {UNUSABLE_BELOW_HZ / 1e6:.0f} MSPS were silently refused "
            "and the device kept its previous rate (measured 2026-09-27)"
        )

    gain = parse_range(attribute("hardwaregain_available"))
    bandwidth = parse_range(attribute("rf_bandwidth_available"))
    bits = parse_sample_format(
        _read(runner, ["iio_info", "-u", uri], timeout_s),
    )
    if bits is None:
        notes.append("the sample format was not readable, so bytes per sample is unknown")

    return RfCapabilities(
        min_rate_hz=rate[0] if rate else None,
        max_rate_hz=rate[2] if rate else None,
        rate_step_hz=rate[1] if rate else None,
        min_gain_db=gain[0] if gain else None,
        max_gain_db=gain[2] if gain else None,
        gain_step_db=gain[1] if gain else None,
        max_bandwidth_hz=bandwidth[2] if bandwidth else None,
        bits=bits[0] if bits else None,
        bytes_per_complex_sample=(2 * bits[1] // 8) if bits else None,
        notes=tuple(notes),
    )
