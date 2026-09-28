"""Wideband band scanning, driven by the native ``rtl_power`` tool.

OrcMesh's other spectrum view shows what one dongle can hear at once, which for
an RTL-SDR is about 2.4 MHz. This module covers a whole region band instead —
902-928 MHz for the US — so it can answer the questions that need the full
picture: which of the mesh's channel slots actually carry traffic, and where in
the band the energy really is.

Why ``rtl_power`` and not ``rtl_sdr`` in a loop: each ``rtl_sdr`` process costs
roughly 3 seconds of start-up. That was measured on the RTL-SDR Blog V4 this was
built against — 1 second of samples took 4.1 seconds of wall time, nearly all of
it device open and tuner lock. Sweeping 26 MHz in 2.4 MHz steps that way would
cost over half a minute per sweep. ``rtl_power`` retunes inside one process and
emits the span already stitched; on this hardware a full 902-928 MHz sweep takes
about a second.

Its output is CSV on stdout, one row per sub-band per interval:

    2026-09-27, 08:41:30, 902000000, 904600000, 81250.00, 256, -5.03, ...

The columns that matter are low_hz, high_hz, step_hz, samples, then one power
value per bin. Rows tile exactly — one row's high_hz is the next one's low_hz —
and a sweep is the run of rows that ascends in frequency, so it is complete when
the frequency drops back down.
"""
from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from meshchat.services import rtl_tools

log = logging.getLogger(__name__)

#: rtl_power writes this for a bin it could not compute. Observed live on a
#: Blog V4, intermittently, as runs of adjacent bins — usually where the tuner
#: was retuning and the sample buffer came up short. bool(float(...)) refuses
#: it, so it has to be recognised by name rather than by trying to parse it.
_NAN_TOKENS = frozenset({
    "-nan(ind)", "nan(ind)", "-nan", "nan", "n/a", "-inf", "inf", "-infinity", "infinity",
})

#: Columns before the first power value: date, time, low, high, step, samples.
_HEADER_FIELDS = 6

#: Rows to hold while assembling a sweep. A 26 MHz span arrives as ~10 rows, so
#: this is generous; it exists so a span that never wraps (or a tool that never
#: restarts its sweep) cannot grow the buffer without bound.
_MAX_PENDING_ROWS = 64


@dataclass(frozen=True)
class PowerRow:
    """One CSV line from rtl_power: a slice of the swept band."""

    low_hz: float
    high_hz: float
    step_hz: float
    samples: int
    power_db: np.ndarray

    @property
    def bin_count(self) -> int:
        return int(self.power_db.size)

    @property
    def frequencies(self) -> np.ndarray:
        """Centre frequency of every bin, in Hz."""
        return self.low_hz + np.arange(self.bin_count) * self.step_hz


@dataclass(frozen=True)
class ScanRequest:
    """What to sweep. Passed to the worker as one object rather than as five
    positional arguments, which is what a queued cross-thread invoke wants."""

    low_hz: float
    high_hz: float
    bin_hz: float = 125_000.0
    interval_s: float = 1.0
    #: Not auto gain, deliberately. "Automatic" on an RTL2832U resolves to
    #: something near maximum, which is the worst case for a survey: measured on
    #: a Blog V4 at 915 MHz, the noise floor sat at +17 dB with auto gain and
    #: -17 dB at 3.7 dB, so auto leaves the least headroom for a strong local
    #: signal before it clips. 16 dB sits mid-table (the tuner snaps to 15.7)
    #: with ample room either way. The tuner's own gain list is discrete, so an
    #: arbitrary value is snapped to the nearest supported step.
    gain_db: float = 16.0
    #: Which dongle to sweep. Named explicitly so a scan can run on one dongle while
    #: a capture runs on another; see rtl_tools.list_devices.
    device_index: int = 0

    @property
    def span_hz(self) -> float:
        return self.high_hz - self.low_hz


def _parse_power(token: str) -> float:
    """One power value, with rtl_power's non-numbers mapped to NaN."""
    if token.lower() in _NAN_TOKENS:
        return float("nan")
    try:
        return float(token)
    except ValueError:
        return float("nan")


def parse_power_row(line: str) -> PowerRow | None:
    """Parse one rtl_power CSV line, or return None if it is not one.

    Returning None rather than raising is deliberate: this runs on whatever the
    tool happens to write, and a single malformed line should cost one row, not
    the whole scan.
    """
    fields = [field.strip() for field in line.split(",")]
    if len(fields) < _HEADER_FIELDS + 2:
        return None
    try:
        low_hz = float(fields[2])
        high_hz = float(fields[3])
        step_hz = float(fields[4])
        samples = int(float(fields[5]))
    except ValueError:
        return None
    if step_hz <= 0 or high_hz < low_hz:
        return None

    values = np.array([_parse_power(field) for field in fields[_HEADER_FIELDS:]], dtype=np.float32)
    if values.size < 2:
        return None
    return PowerRow(low_hz, high_hz, step_hz, samples, values)


def merge_rows(rows: Sequence[PowerRow]) -> PowerRow:
    """Stitch sub-band rows onto one uniform grid over their union span.

    Bins rtl_power could not compute are skipped rather than propagated. That
    matters more than it looks: consecutive rows share an endpoint frequency,
    and the shared bin is exactly where a row's own edge value tends to be the
    unusable one, so letting a neighbour fill it is what turns rtl_power's gaps
    into a continuous spectrum. Leaving a genuine hole as NaN is honest — the
    display can show a gap — where inventing a value would not be.
    """
    if not rows:
        raise ValueError("nothing to merge")

    step_hz = rows[0].step_hz
    low_hz = min(row.low_hz for row in rows)
    high_hz = max(row.high_hz for row in rows)
    # Inclusive of both ends: a 2.6 MHz row at 81.25 kHz is 33 bins, not 32.
    count = int(round((high_hz - low_hz) / step_hz)) + 1
    merged = np.full(count, np.nan, dtype=np.float32)

    for row in rows:
        if row.step_hz != step_hz:
            # A differing step would shear the axis against every other row.
            # Dropping it leaves an obviously missing band; resampling would
            # silently plot power at frequencies it was never measured at.
            log.warning(
                "Dropping scan row with step %.1f Hz (expected %.1f Hz)", row.step_hz, step_hz,
            )
            continue
        start = int(round((row.low_hz - low_hz) / step_hz))
        for offset, value in enumerate(row.power_db):
            if not np.isfinite(value):
                continue
            index = start + offset
            if 0 <= index < count and not np.isfinite(merged[index]):
                merged[index] = value

    return PowerRow(low_hz, low_hz + (count - 1) * step_hz, step_hz, rows[0].samples, merged)


@dataclass(frozen=True)
class BandPicture:
    """A band with the gaps filled in from earlier sweeps.

    `row` holds NaN only where a bin has genuinely never been measured (or has
    gone stale); `coverage` says what fraction is real, so a caller can show
    how complete the picture is instead of implying it is whole.
    """

    row: PowerRow
    coverage: float
    sweeps: int
    ages: np.ndarray


class BandAccumulator:
    """Fill rtl_power's holes across sweeps by remembering each bin's last value.

    rtl_power loses a couple of percent of bins every sweep, scattered about and
    different each time — its FFT shares a thread with acquisition, so a busy
    moment drops samples and those bins come back as -nan(ind). Measured on a
    Blog V4 over 902-928 MHz: 2.6% lost with a hamming window, and no flag gets
    it better (single-shot mode is worse, at 47.9%, because every start refills
    the buffers from scratch).

    Rather than leave permanent holes in the band, each bin keeps its most
    recent real measurement — what a spectrum analyzer's persistence display has
    always done. One sweep is ~97% complete, so within a few seconds every bin
    has been measured at least once. Nothing is invented: `ages` records how
    many sweeps ago each bin was last seen, so a stale bin can be drawn
    differently and a bin past `max_age` reverts to NaN rather than being
    presented as current.
    """

    def __init__(self, max_age: int = 30) -> None:
        self._max_age = max_age
        self._low_hz = 0.0
        self._high_hz = 0.0
        self._step_hz = 0.0
        self._samples = 0
        self._power = np.zeros(0, dtype=np.float32)
        self._ages = np.zeros(0, dtype=np.int32)
        self._sweeps = 0

    @property
    def sweeps(self) -> int:
        return self._sweeps

    def _geometry(self, sweep: PowerRow) -> tuple[float, float, float]:
        return (sweep.low_hz, sweep.high_hz, sweep.step_hz)

    def update(self, sweep: PowerRow) -> BandPicture:
        """Fold one sweep in; return the band as it now stands."""
        geometry = self._geometry(sweep)
        if self._power.size != sweep.bin_count or geometry != (
            self._low_hz, self._high_hz, self._step_hz
        ):
            # A different span or step is a different picture, not a refinement
            # of this one — merging them would place power at frequencies it
            # was never measured at.
            self._low_hz, self._high_hz, self._step_hz = geometry
            self._samples = sweep.samples
            self._power = np.full(sweep.bin_count, np.nan, dtype=np.float32)
            self._ages = np.full(sweep.bin_count, -1, dtype=np.int32)
            self._sweeps = 0

        self._sweeps += 1
        # -1 marks "never measured" and must stay distinct from "old", so the
        # age of an unmeasured bin is left alone rather than incremented.
        self._ages[self._ages >= 0] += 1
        measured = np.isfinite(sweep.power_db)
        self._power[measured] = sweep.power_db[measured]
        self._ages[measured] = 0

        # A bin that has not been measurable for a long time goes back to NaN:
        # holding it forever would keep presenting a stale reading as current.
        stale = self._ages > self._max_age
        self._power[stale] = np.nan

        fresh = int(np.isfinite(self._power).sum())
        coverage = fresh / self._power.size if self._power.size else 0.0
        # Copied, not handed out by reference: these arrays are mutated in place
        # by the next update, so an emitted picture would otherwise silently
        # change under whoever is holding it — a redraw would show the newest
        # sweep under the caption of an older one, and any asserted history
        # would be worthless. PowerRow being frozen does not help here, since
        # that only protects the reference, not the array's contents.
        row = PowerRow(
            self._low_hz, self._high_hz, self._step_hz, self._samples,
            self._power.copy(),
        )
        return BandPicture(
            row=row, coverage=coverage, sweeps=self._sweeps, ages=self._ages.copy(),
        )


class ScanAssembler:
    """Turn rtl_power's stream of sub-band rows into whole-band sweeps."""

    def __init__(self, max_rows: int = _MAX_PENDING_ROWS) -> None:
        self._rows: list[PowerRow] = []
        self._max_rows = max_rows

    @property
    def pending_rows(self) -> int:
        return len(self._rows)

    def add(self, row: PowerRow) -> PowerRow | None:
        """Add a row; return the finished sweep when this row starts a new one.

        A sweep is complete when the frequency stops ascending. The returned
        sweep is the *previous* run of rows, so the first full sweep appears
        one interval after the second one begins — the alternative is emitting
        a partial spectrum that looks like a band with nothing in it.
        """
        finished: PowerRow | None = None
        if self._rows and row.low_hz <= self._rows[-1].low_hz:
            finished = merge_rows(self._rows)
            self._rows = []
        self._rows.append(row)
        if len(self._rows) > self._max_rows:
            del self._rows[:-self._max_rows]
        return finished


class ScanWorker(QObject):
    """Blocking rtl_power reads live here, never on the GUI thread."""

    sweep_ready = Signal(object)            # PowerRow, stitched over the sweep
    band_ready = Signal(object)             # BandPicture, gaps filled in
    started = Signal(float, float, float)   # low_hz, high_hz, bin_hz
    stopped = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None, *, label: str = "the band scan"):
        super().__init__(parent)
        self._label = label
        self._owner = f"scan-{id(self)}"
        self._proc: subprocess.Popen[bytes] | None = None
        #: Which dongle this worker last claimed, for releasing the same lease.
        self._device = 0
        self._stderr: rtl_tools.StderrCollector | None = None
        self._running = False
        self._assembler = ScanAssembler()
        self._band = BandAccumulator()

    def _command(self, request: ScanRequest) -> list[str]:
        tool = rtl_tools.find_tool("rtl_power")
        if tool is None:
            raise FileNotFoundError(
                "rtl_power was not found on PATH, so the band cannot be scanned."
            )
        gain = "0" if request.gain_db < 0 else f"{request.gain_db:.1f}"
        # No -e: the scan ends when it is told to, not on a timer.
        return [
            str(tool),
            "-d", str(int(request.device_index)),
            "-f", f"{int(request.low_hz)}:{int(request.high_hz)}:{int(request.bin_hz)}",
            "-g", gain,
            "-i", f"{request.interval_s:g}",
            # rtl_power defaults to a rectangular window, which leaks so badly
            # that bins come back unusable all over the band. Measured on a
            # Blog V4 over 902-928 MHz at gain 20: rectangle left 41% of bins
            # as -nan(ind), hamming left 2%. The documented alternative — the
            # FIR downsample filter with -c 50% — got only to 11% and halved
            # the bins per row, because cropping discards half the FFT.
            "-w", "hamming",
            "-",  # dump the CSV to stdout
        ]

    @Slot(object)
    def start(self, request: ScanRequest) -> None:
        if self._running:
            return
        try:
            command = self._command(request)
        except FileNotFoundError as exc:
            self.error.emit(str(exc))
            return

        self._device = request.device_index
        held, complaint = rtl_tools.acquire_sdr(
            self._owner, self._label, request.device_index
        )
        if not held:
            self.error.emit(complaint)
            return

        try:
            self._proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=rtl_tools.creation_flags(),
            )
        except OSError as exc:
            # See sdr_source.start: the lease is taken before the spawn, so a spawn
            # that failed has to release it or the dongle stays busy until restart.
            rtl_tools.release_sdr(self._owner, request.device_index)
            log.exception("Could not start rtl_power")
            self.error.emit(f"Could not start rtl_power: {exc}")
            return

        self._stderr = rtl_tools.StderrCollector(self._proc.stderr)
        self._assembler = ScanAssembler()
        self._band = BandAccumulator()
        self._running = True
        self.started.emit(request.low_hz, request.high_hz, request.bin_hz)
        self._scan_loop()

    def _scan_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        while self._running:
            # readline() blocks until a line arrives and returns b'' at EOF,
            # which is how the tool exiting is noticed.
            raw = proc.stdout.readline()
            if not raw:
                break
            row = parse_power_row(raw.decode("utf-8", errors="replace"))
            if row is None:
                continue
            sweep = self._assembler.add(row)
            if sweep is not None:
                self.sweep_ready.emit(sweep)
                # Emitted as well as the raw sweep: one sweep is ~97% complete,
                # so the accumulated picture is what a band view should draw.
                self.band_ready.emit(self._band.update(sweep))

        # As in the capture worker: a deliberate stop and a clean self-exit are
        # both normal, and stderr cannot tell them apart.
        intentional = not self._running
        tail, code = self._close()
        if intentional or code == 0:
            self.stopped.emit("Scan stopped")
            return
        self.error.emit(rtl_tools.explain_failure(tail, tool="rtl_power"))

    @Slot()
    def stop(self) -> None:
        """Ask the scan to end. Safe to call from the GUI thread.

        Terminating is what unblocks the readline() the worker is sitting in.
        """
        self._running = False
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                log.warning("Could not terminate rtl_power", exc_info=True)

    def _close(self) -> tuple[str, int | None]:
        """Tear the child process down; return (last thing it said, exit code)."""
        proc, self._proc = self._proc, None
        rtl_tools.release_sdr(self._owner, self._device)
        code: int | None = proc.poll() if proc is not None else None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    code = proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    log.warning("rtl_power ignored terminate(); killing it")
                    proc.kill()
                    try:
                        code = proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        log.error("rtl_power could not be killed")
                        code = None
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

        collector, self._stderr = self._stderr, None
        if collector is not None:
            collector.join(timeout=1)
            tail = collector.tail()
        else:
            tail = ""
        return tail, code

    def stderr_tail(self) -> str:
        collector = self._stderr
        return collector.text() if collector is not None else ""

    def shutdown(self) -> None:
        self.stop()
        self._close()


class ScanController(QObject):
    """GUI-facing facade; owns the worker thread."""

    sweep_ready = Signal(object)
    band_ready = Signal(object)
    started = Signal(float, float, float)
    stopped = Signal(str)
    error = Signal(str)
    #: Internal: carries a ScanRequest to the worker on its own thread. A queued
    #: signal rather than QMetaObject.invokeMethod with Q_ARG(object, request),
    #: which PySide6 cannot marshal — "object" is not a registered meta type, so
    #: the call raised RuntimeError on the first click of "Scan region".
    _start_requested = Signal(object)

    def __init__(self, parent=None, *, label: str = "the band scan"):
        super().__init__(parent)
        self._worker = ScanWorker(label=label)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        self._worker.sweep_ready.connect(self.sweep_ready)
        self._worker.band_ready.connect(self.band_ready)
        self._worker.started.connect(self.started)
        self._worker.stopped.connect(self.stopped)
        self._worker.error.connect(self.error)
        # Queued explicitly: the request must run on the worker's thread, and the
        # worker sits in a blocking read there.
        self._start_requested.connect(
            self._worker.start, Qt.ConnectionType.QueuedConnection
        )

        self._thread.start()

    def start(self, request: ScanRequest) -> None:
        self._start_requested.emit(request)

    def stop(self) -> None:
        # Direct call (not queued): the worker is inside its blocking read
        # loop, so a queued invocation would not be processed until it exits.
        self._worker.stop()

    def shutdown(self) -> None:
        self.stop()
        self._thread.quit()
        self._thread.wait(3000)

    def stderr_tail(self) -> str:
        return self._worker.stderr_tail()
