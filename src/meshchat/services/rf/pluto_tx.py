"""Putting a tone on the air from the Pluto — the only code here that transmits.

Everything else in OrcMesh either listens or manages a device. This deliberately does not,
so it is written to be dull, explicit and reversible:

**It uses the hardware tone generator, not a stream.** The AD9361 has DDS cores that
synthesise a tone in the FPGA fabric (`cf-ad9361-dds-core-lpc`). A continuous carrier
therefore costs no USB bandwidth, no buffer scheduling, and no CPU — there is nothing to
underrun, so the tone cannot develop gaps. Measured on this board: `altvoltage0`
(`TX1_I_F1`) and `altvoltage2` (`TX1_Q_F1`), each with `frequency`, `phase` and `scale`.

**The tone is a baseband offset from the TX local oscillator, never an absolute
frequency.** The LO is set to the target minus the offset and the DDS is asked for the
offset. Two consequences worth knowing:

* The offset must be well under the DDS `sampling_frequency` (30.72 MHz here, so Nyquist is
  15.36 MHz). 1 MHz is comfortable.
* A tone generated at **zero** offset would be indistinguishable from the board's own LO
  leakage, which is always present at the LO frequency. That would make the tone useless for
  the one thing a signal source is for: proving it is actually transmitting. An offset makes
  the product a real mixing product rather than something that might have been there anyway.

**I and Q are both driven, in quadrature.** A single tone on I alone is not a carrier: it is
a real baseband signal, and upconverting one of those produces *two* tones, mirrored about
the LO. Driving the pair in quadrature cancels the unwanted sideband. `I` is set to phase 0
and `Q` to 90 degrees.

**What 75% means, precisely.** `amplitude` is a fraction of full-scale DDS drive, so 0.75 is
75% of the signal — **-2.5 dB**, not -1.25 dB. Powerful is not the same as loud: a 75% duty
of *power* would be -1.25 dB, and the two are a factor of 1.4 apart in amplitude. The number
is a named constant in one place so the interpretation can be changed without hunting.

**It puts the hardware back.** The LO frequency and the TX attenuation are read before
anything is written and restored afterwards, because a test that changes hardware state and
walks away leaves the next one measuring the wrong thing.

**A tone outlives the process that started it.** The DDS keeps generating after OrcMesh is
gone — a hard kill, a crash, a Task Manager end. `stop()` is registered with `atexit` for
every other case, and a watchdog stops the tone on its own at the requested duration even if
the caller never does. Neither covers a hard kill, and that is stated rather than implied:
if the tone is still on the air after OrcMesh dies, the only ways to silence it are
`stop_tone()` from a fresh process, resetting the board, or power-cycling it.
"""
from __future__ import annotations

import atexit
import logging
import math
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

log = logging.getLogger(__name__)

#: The TX local oscillator and the physical transmit channel, as this board names them.
#: Measured, not assumed: `altvoltage1` reports `id 'TX_LO'`, and the `voltage0` **output**
#: channel is the one carrying `hardwaregain` with a range of -89.75 .. 0 dB.
TX_LO_CHANNEL = "altvoltage1"
TX_CHANNEL = "voltage0"
PHY_DEVICE = "ad9361-phy"
DDS_DEVICE = "cf-ad9361-dds-core-lpc"

#: `voltage0` exists as **both** an input and an output on `ad9361-phy`: the receive gain and
#: the transmit attenuator share the name. Without this flag `iio_attr` reads whichever it
#: reaches first and prints both values, and a write is ambiguous — measured, setting
#: `hardwaregain 0` without it was refused outright, so the transmit path silently did not
#: work while the code looked correct. Every access to the transmit channel carries the flag.
PHY_TX_FLAG = "-o"

#: The quadrature pair. Both must be driven for a single tone — see the module docstring.
DDS_I_CHANNEL = "altvoltage0"  # id TX1_I_F1
DDS_Q_CHANNEL = "altvoltage2"  # id TX1_Q_F1

#: Maximum attenuation on this board. Used as the mute, and as the value a stop restores to
#: when nothing was recorded beforehand.
MUTED_ATTENUATION_DB = -89.75

#: Baseband offset of the tone from the LO. See the module docstring for why it is not zero.
DEFAULT_OFFSET_HZ = 1_000_000

#: 75% of full-scale DDS drive. In decibels that is -2.5 dB, because power follows amplitude
#: squared and the two are easy to confuse.
TONE_AMPLITUDE = 0.75

#: The ceiling on drive, and the reason it is 89 rather than 90: the requirement is to stay
#: *below* 90%, and a cap at exactly 90 leaves nothing for a rounding step to be wrong by.
#: One whole percentage point of margin costs 0.09 dB, which is nothing on the air.
#:
#: Enforced here rather than in the UI, because a control's limit is a suggestion and this is
#: supposed to be a guarantee: a request built anywhere in the program is brought down to
#: this, and the clamp is logged so it is never changed behind anyone's back in silence.
MAX_TX_DRIVE = 0.89


def _db(fraction: float) -> float:
    """A drive fraction in decibels, relative to full scale."""
    return 20 * math.log10(fraction) if fraction > 0 else float("-inf")


TONE_AMPLITUDE_DB = _db(TONE_AMPLITUDE)

DURATION_S = 30.0

#: A single `iio_attr` write over the network link. Generous next to the ~33 ms a retune
#: costs, because these are attribute writes on a board reached over Ethernet.
WRITE_TIMEOUT_S = 5.0

_calls: Callable[[Sequence[str], float], int] | None = None


@dataclass(frozen=True)
class ToneRequest:
    """What to put on the air. Kept separate from the writes so it can be shown first.

    `amplitude` is a fraction of full-scale drive, and is brought down to `MAX_TX_DRIVE` if a
    caller asks for more. The clamp lives here so it holds for every path into the
    transmitter — a dial, a script, a test — rather than only for the one with a slider on it.
    """

    frequency_hz: float
    duration_s: float = DURATION_S
    amplitude: float = TONE_AMPLITUDE
    attenuation_db: float = 0.0
    offset_hz: float = DEFAULT_OFFSET_HZ

    def __post_init__(self) -> None:
        if self.amplitude > MAX_TX_DRIVE:
            log.warning(
                "Drive of %.3f asked for, clamped to the %.2f ceiling",
                self.amplitude, MAX_TX_DRIVE,
            )
            object.__setattr__(self, "amplitude", MAX_TX_DRIVE)
        elif self.amplitude < 0.0:
            object.__setattr__(self, "amplitude", 0.0)

    @property
    def local_oscillator_hz(self) -> float:
        return self.frequency_hz - self.offset_hz

    @property
    def amplitude_db(self) -> float:
        """Drive relative to full scale. -2.5 dB at the default 0.75, not -1.25.

        A *fraction of amplitude*, so this is also the power figure in dB — which is exactly
        where the confusion lives: 75% of amplitude is -2.5 dB, while 75% of *power* would be
        -1.25 dB. These numbers are amplitude fractions.
        """
        return _db(self.amplitude)

    @property
    def power_db(self) -> float:
        """Alias kept explicit so a caller reaching for power finds the same number."""
        return self.amplitude_db


def _default_run(args: Sequence[str], timeout_s: float) -> int:
    """Run one `iio_attr` write and return its exit code.

    Exit status is what decides success — never the text. `iio_attr` writes to stderr on a
    refusal, and on Windows a process that exits 0 is still falsy in Python, so the code is
    compared explicitly everywhere it is used.
    """
    try:
        completed = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout_s, errors="replace",
        )
    except subprocess.TimeoutExpired:
        log.warning("%s did not finish within %.1f s", args[0], timeout_s)
        return -1
    except OSError:
        log.exception("Could not run %s", args[0])
        return -1
    if completed.returncode != 0:
        log.warning(
            "iio_attr refused: %s\n%s", " ".join(str(p) for p in args[1:]),
            (completed.stderr or "").strip(),
        )
    return completed.returncode


@dataclass(frozen=True)
class TonePlan:
    """Every write that will be made, in order, so it can be inspected before it happens.

    `read` entries are the state to be restored; they are read before anything is written
    so that a failed start cannot leave the board changed and unrecorded.
    """

    request: ToneRequest
    uri: str
    writes: tuple[tuple[str, ...], ...]
    reads: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    def describe(self) -> str:
        return (
            f"{self.request.frequency_hz / 1e6:.6f} MHz for {self.request.duration_s:g} s "
            f"at {self.request.amplitude * 100:.0f}% drive "
            f"({self.request.amplitude_db:+.1f} dB), TX {self.request.attenuation_db:+.2f} dB "
            f"attenuation"
        )


def _attr(
    uri: str,
    device: str,
    channel: str,
    *pairs: tuple[str, str],
    flag: str | None = None,
) -> list[tuple[str, ...]]:
    writes: list[tuple[str, ...]] = []
    for name, value in pairs:
        args: tuple[str, ...] = ("iio_attr", "-u", uri, "-c", device)
        if flag is not None:
            args += (flag,)
        args += (channel, name, value)
        writes.append(args)
    return writes


def plan_tone(request: ToneRequest, *, uri: str) -> TonePlan:
    """Work out the whole sequence, including what to put back afterwards.

    Ordering matters. The LO and the attenuation go first so the board is already tuned and
    attenuated when the tone appears, and the **I and Q scales go last of all** — that is the
    step that actually puts energy on the air, so nothing after it should be able to fail.
    """
    reads = (
        ("iio_attr", "-u", uri, "-c", PHY_DEVICE, TX_CHANNEL, "hardwaregain"),
        ("iio_attr", "-u", uri, "-c", PHY_DEVICE, TX_LO_CHANNEL, "frequency"),
    )
    writes: list[tuple[str, ...]] = []
    writes += _attr(uri, PHY_DEVICE, TX_LO_CHANNEL, ("frequency", f"{request.local_oscillator_hz:.0f}"))
    writes += _attr(
        uri, PHY_DEVICE, TX_CHANNEL, ("hardwaregain", f"{request.attenuation_db:.6f}"),
        flag=PHY_TX_FLAG,
    )
    writes += _attr(
        uri, DDS_DEVICE, DDS_I_CHANNEL,
        ("frequency", f"{request.offset_hz:.0f}"), ("phase", "0"),
    )
    writes += _attr(
        uri, DDS_DEVICE, DDS_Q_CHANNEL,
        ("frequency", f"{request.offset_hz:.0f}"), ("phase", "90000"),
    )
    writes += _attr(uri, DDS_DEVICE, DDS_I_CHANNEL, ("scale", f"{request.amplitude:.6f}"))
    writes += _attr(uri, DDS_DEVICE, DDS_Q_CHANNEL, ("scale", f"{request.amplitude:.6f}"))
    return TonePlan(request=request, uri=uri, writes=tuple(writes), reads=reads)


def mute_plan(uri: str, *, restore_attenuation_db: float | None = None) -> TonePlan:
    """Zero the tone, and put the transmit chain back the way it was found.

    Both halves are necessary. Zeroing the DDS scales stops the tone; restoring the
    attenuation stops the transmitter amplifying whatever DC the DAC happens to be holding.
    Doing only the first leaves a live, unattenuated transmit chain.
    """
    attenuation = (
        MUTED_ATTENUATION_DB if restore_attenuation_db is None else restore_attenuation_db
    )
    writes: list[tuple[str, ...]] = []
    writes += _attr(uri, DDS_DEVICE, DDS_I_CHANNEL, ("scale", "0"))
    writes += _attr(uri, DDS_DEVICE, DDS_Q_CHANNEL, ("scale", "0"))
    writes += _attr(
        uri, PHY_DEVICE, TX_CHANNEL, ("hardwaregain", f"{attenuation:.6f}"), flag=PHY_TX_FLAG,
    )
    return TonePlan(
        request=ToneRequest(frequency_hz=0.0, duration_s=0.0),
        uri=uri,
        writes=tuple(writes),
    )


def read_back(uri: str) -> dict[str, str]:
    """Read the current transmit state, for reporting rather than for deciding.

    Best effort by design: a missing reading is reported as an empty string rather than
    raised, because this is used to describe a state, not to guard one.
    """
    values: dict[str, str] = {}
    for key, args in {
        "i": ("iio_attr", "-u", uri, "-c", DDS_DEVICE, DDS_I_CHANNEL, "scale"),
        "q": ("iio_attr", "-u", uri, "-c", DDS_DEVICE, DDS_Q_CHANNEL, "scale"),
        "attenuation": (
            "iio_attr", "-u", uri, "-c", PHY_DEVICE, PHY_TX_FLAG, TX_CHANNEL, "hardwaregain",
        ),
        "tx_lo": ("iio_attr", "-u", uri, "-c", PHY_DEVICE, TX_LO_CHANNEL, "frequency"),
    }.items():
        try:
            completed = subprocess.run(
                list(args), capture_output=True, text=True, timeout=WRITE_TIMEOUT_S,
                errors="replace",
            )
            text = (completed.stdout or "").strip()
            values[key] = text.splitlines()[-1].split()[-1] if text else ""
        except (OSError, subprocess.SubprocessError):
            values[key] = ""
    return values


class ToneTransmitter:
    """Starts one tone, stops it, and refuses to leave it running unattended.

    One at a time, per process. Two concurrent tones would fight over the same DDS pair, so
    the second `start()` is refused rather than merged.
    """

    def __init__(
        self,
        uri: str,
        *,
        run: Callable[[Sequence[str], float], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.uri = uri
        self._run = run or _default_run
        self._clock = clock
        self._lock = threading.Lock()
        self._plan: TonePlan | None = None
        self._watchdog: threading.Timer | None = None
        self._original_attenuation: float | None = None
        self._started_at: float | None = None

    @property
    def is_transmitting(self) -> bool:
        return self._plan is not None

    def started_at(self) -> float | None:
        return self._started_at

    def _write(self, args: Sequence[str]) -> int:
        return self._run(args, WRITE_TIMEOUT_S)

    def _first_value(self, args: Sequence[str]) -> str:
        """Read one attribute's value. Empty string means it could not be read."""
        try:
            completed = subprocess.run(
                list(args), capture_output=True, text=True, timeout=WRITE_TIMEOUT_S,
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        text = (completed.stdout or "").strip()
        return text.splitlines()[-1].split()[-1] if text else ""

    def start(self, request: ToneRequest, *, automatic_stop: bool = True) -> TonePlan:
        """Put the tone on the air. Raises `AlreadyTransmitting` if one is running.

        A failed write aborts and **mutes**, because a half-written chain is a transmitter in
        an unknown state — possibly radiating, possibly at full attenuation, and definitely
        not what was asked for.
        """
        with self._lock:
            if self._plan is not None:
                raise AlreadyTransmitting(
                    "A tone is already transmitting. Stop it before starting another.",
                )
            plan = plan_tone(request, uri=self.uri)
            self._original_attenuation = self._read_attenuation()
            for args in plan.writes:
                if self._write(args) != 0:
                    log.error("Tone start failed on: %s", " ".join(str(p) for p in args[1:]))
                    self._mute_locked()
                    raise ToneFailed(
                        "The Pluto refused a write while starting the tone, so nothing was "
                        "transmitted (or anything that was is now muted). Check the board is "
                        "reachable and not already transmitting.",
                    )
            self._plan = plan
            self._started_at = self._clock()
            if automatic_stop:
                self._arm_watchdog(request.duration_s)
            # Registered here rather than only in `start_tone`, so a page that owns its own
            # transmitter is still silenced by a clean exit or an unhandled exception.
            global _active
            _active = self
            _hook_atexit()
            log.warning("Tone on the air: %s", plan.describe())
            return plan

    def _read_attenuation(self) -> float | None:
        """The transmit attenuator, read from the OUTPUT channel specifically.

        Addressed with the direction flag rather than leaving it out and taking the last
        line: the unflagged read returns the receive gain as well, so the earlier version
        only appeared to work by accident of which line came last.
        """
        raw = self._first_value(
            ("iio_attr", "-u", self.uri, "-c", PHY_DEVICE, PHY_TX_FLAG, TX_CHANNEL,
             "hardwaregain"),
        )
        try:
            return float(raw.split()[0])
        except (ValueError, IndexError):
            return None

    def _arm_watchdog(self, duration_s: float) -> None:
        """Stop the tone even if nobody else does.

        A transmit that depends on its caller remembering to stop it is a transmit that can
        be left on the air by a UI bug. The caller is given the full duration plus a second
        of slack to stop it first, so the normal path is still the caller's.
        """
        self._watchdog = threading.Timer(max(0.0, duration_s) + 1.0, self._watchdog_fired)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _watchdog_fired(self) -> None:
        log.warning("Tone reached its duration with nobody stopping it; muting")
        self.stop()

    def set_drive(self, fraction: float) -> float:
        """Change the drive on a **running** tone, and report what was actually applied.

        Refuses when nothing is transmitting, and that is a safety property rather than
        tidiness: the scale attribute is the last thing written when starting a tone,
        precisely because it is the step that puts energy on the air. Writing it on its own
        would key the transmitter with the local oscillator and the attenuator still wherever
        they happened to be — which is a tone nobody asked for, at an unknown frequency.

        Both scales are written, Q first, so the pair is never briefly mismatched: a
        quadrature pair driven at two different amplitudes is a carrier plus a reflection of
        itself, which is worse than either level alone.
        """
        with self._lock:
            if self._plan is None:
                raise NotTransmitting(
                    "There is no tone to adjust. Start one first — set_drive() only ever "
                    "changes a carrier that is already on the air.",
                )
            bounded = min(max(fraction, 0.0), MAX_TX_DRIVE)
            if bounded != fraction:
                log.warning(
                    "Drive of %.3f asked for, clamped to the %.2f ceiling",
                    fraction, MAX_TX_DRIVE,
                )
            for channel in (DDS_Q_CHANNEL, DDS_I_CHANNEL):
                self._write(self._attr_args(DDS_DEVICE, channel, ("scale", f"{bounded:.6f}")))
            self._plan = replace(self._plan, request=replace(self._plan.request, amplitude=bounded))
            return bounded

    @property
    def request(self) -> ToneRequest | None:
        """The tone in effect right now, including any drive change made since it started."""
        return self._plan.request if self._plan is not None else None

    def _attr_args(self, device: str, channel: str, *pairs: tuple[str, str]) -> list[str]:
        args = ["iio_attr", "-u", self.uri, "-c", device, channel]
        for name, value in pairs:
            args += [name, value]
        return args

    def _mute_locked(self) -> None:
        for args in mute_plan(self.uri, restore_attenuation_db=self._original_attenuation).writes:
            self._write(args)
        self._plan = None
        self._started_at = None

    def stop(self) -> None:
        """Take the tone off the air and restore the transmit chain. Safe to call twice."""
        global _active
        with self._lock:
            self._cancel_watchdog_locked()
            if _active is self:
                _active = None
            if self._plan is None:
                # Still mute: a stop is also how a user recovers from a tone left running by
                # an earlier crash, when this object never saw a start.
                self._mute_locked()
                return
            self._mute_locked()
            log.warning("Tone off the air")

    def _cancel_watchdog_locked(self) -> None:
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.cancel()


class ToneFailed(RuntimeError):
    """Raised when the board refused a write while starting a tone."""


class AlreadyTransmitting(RuntimeError):
    """Raised when a tone is already running in this process."""


class NotTransmitting(RuntimeError):
    """Raised when something is asked of a tone that is not on the air.

    Exists because the one thing that must never happen is a write to the DDS scale while
    nothing is transmitting: the scale is the step that puts energy out, so writing it with
    the local oscillator and attenuator wherever they happen to be left is a carrier at an
    unknown frequency, which is the worst possible outcome of a mistaken adjustment.
    """


#: The one live transmitter, so `atexit` and a recovery `stop_tone()` can reach it.
_active: ToneTransmitter | None = None
_atexit_hooked = False


def _hook_atexit() -> None:
    global _atexit_hooked
    if _atexit_hooked:
        return
    atexit.register(stop_tone)
    _atexit_hooked = True


def start_tone(request: ToneRequest, *, uri: str, **kwargs) -> TonePlan:
    """Start the process-wide tone. See `ToneTransmitter.start` for the safety properties."""
    return ToneTransmitter(uri, **kwargs).start(request)


def stop_tone() -> None:
    """Stop the process-wide tone, if there is one. Safe to call when there is not.

    Registered with `atexit`, so a clean exit or an unhandled exception silences the
    transmitter on the way out. A hard kill is not covered — the DDS keeps running, and the
    module docstring says how to silence it.
    """
    global _active
    transmitter, _active = _active, None
    if transmitter is not None:
        transmitter.stop()
