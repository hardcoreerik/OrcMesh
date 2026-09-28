"""MeshChat – RTL-SDR capture source for the spectrum waterfall.

The blocking capture loop lives on its own QThread and emits FFT power rows.
It drives the native ``rtl_sdr`` tool as a child process rather than using a
Python binding — see ``rtl_tools.py`` for why that is the right call for an
RTL-SDR Blog V4 — and turns the tool's 8-bit I/Q stream into the same rows
this module has always emitted, so neither the Spectrum nor the SIGINT page
cares how the samples arrive.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Slot

from meshchat.services import rtl_tools
from meshchat.services.iq_recorder import CaptureError, IqRecorder, iter_capture_chunks

log = logging.getLogger(__name__)

FFT_BINS = 1024

#: Samples per read: 32768 is 16 ms at 2.048 MS/s, so rows arrive at roughly
#: 60/s — fast enough that the waterfall looks live, slow enough that the
#: per-row array shift and image upload stay off the GUI thread's critical
#: path. Each read is averaged into one row (32 FFTs at FFT_BINS each).
_READ_SAMPLES = 32768

#: How much of the tool's stderr to keep for diagnostics. The banner is a few
#: lines; the useful message is whichever line came last before it exited.
_STDERR_LINES = 40

#: Bins the tuner's DC term occupies, as an odd count centred on the middle.
#:
#: Not a guess and not a tuning knob: an RTL-SDR's local oscillator leaks into its own
#: mixer, which puts a constant offset on the I/Q that lands in the centre bin of every
#: FFT row, and a Hann window spreads that term onto three bins — the centre plus one
#: either side at exactly -6 dB, with nothing at +-2. Measured on a Blog V4 with a 5 LSB
#: offset: +15.6 dB on the centre, +9.9 and +10.1 dB either side.
#:
#: It matters more than a single ugly bin, because a term that never changes draws a
#: *plateau* down the time axis of the 3D view — a wall of false signal standing in the
#: middle of the band, which is where the channel of interest usually is.
DC_ARTEFACT_BINS = 3


def sdr_available() -> tuple[bool, str]:
    """Return (available, reason). Checks for the native tools only.

    The device itself is not opened here — that is what makes trouble with a
    busy dongle indistinguishable from no dongle at all, and it costs seconds.
    ``rtl_tools.probe_device()`` is the explicit check for that.
    """
    return rtl_tools.tools_available()


#: How far above its neighbours the centre has to stand before it is treated as a real
#: signal rather than the residue of the offset that was just removed. The same 3 dB the
#: DC artefact itself was measured at.
_CENTRE_SIGNAL_DB = 3.0


def _repair_dc_bins(row_db: np.ndarray) -> np.ndarray:
    """Draw a straight line across the bins that removing the DC term left a notch in.

    Subtracting each frame's mean takes the tuner's DC term out, but it does not leave
    the centre looking like the floor: with the offset gone, the centre bin comes back
    about 4.8 dB BELOW its neighbours (measured). That is a trench down the middle of
    the 3D surface where the plateau used to be, and a trench is as wrong as a
    plateau — see DC_ARTEFACT_BINS for why the window makes it three bins wide.

    These bins are therefore synthesised rather than measured, and only when they have
    nothing in them: if the centre still stands 3 dB above its neighbours once the
    offset is gone, something is genuinely transmitting there and the row is left
    alone. That guard matters more here than in most SDR software, because a preset
    tunes the centre of the window onto the channel of interest on purpose.

    The limit that cannot be engineered away: a carrier exactly on the LO frequency is
    a DC term by definition, so the mean subtraction removes it along with the offset.
    Separating those two is what tuning off-centre and shifting the spectrum would be
    for; this does not pretend to.
    """
    half = DC_ARTEFACT_BINS // 2
    centre = row_db.size // 2
    occupied = row_db[centre - half:centre + half + 1]
    reference = (row_db[centre - half - 2] + row_db[centre + half + 2]) / 2.0
    if float(occupied.max()) > reference + _CENTRE_SIGNAL_DB:
        return row_db
    for offset in range(-half, half + 1):
        index = centre + offset
        row_db[index] = (row_db[index - half - 2] + row_db[index + half + 2]) / 2.0
    return row_db


def iq_to_power_row(raw: bytes) -> np.ndarray | None:
    """One chunk of rtl_sdr's 8-bit I/Q stream into one FFT power row, in dB.

    Split out of the capture loop so the scaling convention can be tested
    without a dongle attached. rtl_sdr writes unsigned 8-bit I/Q pairs as
    I0 Q0 I1 Q1 ..., centred on **127.5** — the midpoint of the unsigned range,
    which keeps I and Q symmetric about zero. Centring on 128 instead would
    leave a small negative DC bias; it is a sub-LSB effect, not a dramatic one,
    but 127.5 is the convention every rtl_sdr consumer uses.

    Whatever bias is left is the tuner's own, and it is removed here rather than
    displayed: each frame's mean is subtracted before the window is applied, and the
    bins the window would have put it in are interpolated — see _repair_dc_bins and
    DC_ARTEFACT_BINS.

    Note that "silence" in this encoding is bytes of 127/128, *not* zero, so an
    all-zero stream is a full-scale DC term. It now reads as a flat floor, having
    been removed along with everything else that sits on the centre frequency.

    Returns None when the chunk cannot fill a single FFT frame.
    """
    octets = np.frombuffer(raw, dtype=np.uint8)
    usable = (octets.size // 2) * 2
    if usable < FFT_BINS * 2:
        return None
    pairs = ((octets[:usable].astype(np.float32) - 127.5) / 127.5).reshape(-1, 2)
    samples = pairs[:, 0] + 1j * pairs[:, 1]

    chunks = samples.size // FFT_BINS
    frames = samples[: chunks * FFT_BINS].reshape(chunks, FFT_BINS)
    # Remove each frame's own mean before windowing. A constant offset IS the frame
    # mean, so this is the software equivalent of an AC-coupled front end, and it is
    # the only place a term that never moves can be taken out.
    frames = frames - frames.mean(axis=1, keepdims=True)
    window = np.hanning(FFT_BINS)
    spectra = np.abs(np.fft.fftshift(np.fft.fft(frames * window, axis=1), axes=1)) ** 2
    power = spectra.mean(axis=0).astype(np.float64)
    return _repair_dc_bins(10.0 * np.log10(power + 1e-12)).astype(np.float32)


def replay_rows(
    path: Path,
    *,
    chunk_samples: int = _READ_SAMPLES,
) -> Iterator[np.ndarray]:
    """Re-run a recorded capture through the same FFT it was watched with.

    Frames come out as fast as the CPU allows rather than in real time — the
    original timing is in the capture's own metadata, and a caller wanting the
    waterfall to scroll at the speed it happened can pace the yield itself.
    Because the same `iq_to_power_row` is used, a replay matches the live view
    exactly rather than being a second, subtly different implementation.
    """
    for raw in iter_capture_chunks(Path(path), chunk_samples * 2):
        row = iq_to_power_row(raw)
        if row is not None:
            yield row


class SdrWorker(QObject):
    """Blocking SDR reads live here, never on the GUI thread."""

    row_ready = Signal(object)          # np.ndarray of FFT power in dB
    started = Signal(float, float, int)  # center_hz, span_hz, bins
    stopped = Signal(str)                # reason / status message
    error = Signal(str)
    recording_finished = Signal(object)  # iq_recorder.CaptureInfo
    recording_failed = Signal(str)

    def __init__(self, parent=None, *, label: str = "the spectrum view"):
        super().__init__(parent)
        self._label = label
        self._owner = f"sdr-{id(self)}"
        self._proc: subprocess.Popen[bytes] | None = None
        self._running = False
        #: Which dongle this worker last claimed. Held so stop() can release the
        #: same lease start() took, since the two are separate calls.
        self._device = 0
        #: The V4 has a TCXO, so no correction is normally needed; a knob is
        #: kept because a non-TCXO clone can be tens of ppm out.
        self._ppm = 0
        self._stderr_lines: deque[str] = deque(maxlen=_STDERR_LINES)
        self._stderr_lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None
        self._recorder: IqRecorder | None = None
        #: Set by arm_recording(); the recorder itself is created in start(),
        #: because only then are the centre frequency and rate it must record
        #: into its metadata actually known.
        self._record_to: Path | None = None
        self._record_max_bytes: int | None = None

    def _command(
        self,
        center_hz: float,
        sample_rate_hz: float,
        gain_db: float,
        device_index: int = 0,
    ) -> list[str]:
        tool = rtl_tools.find_tool("rtl_sdr")
        if tool is None:
            raise FileNotFoundError("rtl_sdr was not found on PATH")
        # rtl_sdr's own convention for automatic gain is 0; a negative value
        # is how this API has always spelled "auto".
        gain = "0" if gain_db < 0 else f"{gain_db:.1f}"
        return [
            str(tool),
            # Named even when it is device 0. With more than one dongle attached,
            # "whichever the driver hands back first" is not a decision anyone made,
            # and selecting by serial is impossible here: this machine's two dongles
            # both report SN 00000001.
            "-d", str(int(device_index)),
            "-f", str(int(center_hz)),
            "-s", str(int(sample_rate_hz)),
            "-g", gain,
            "-p", str(int(self._ppm)),
            "-",  # dump IQ to stdout
        ]

    @Slot(float, float, float, int)
    def start(
        self,
        center_hz: float,
        sample_rate_hz: float,
        gain_db: float,
        device_index: int = 0,
    ) -> None:
        if self._running:
            return
        try:
            command = self._command(center_hz, sample_rate_hz, gain_db, device_index)
        except FileNotFoundError as exc:
            self.error.emit(str(exc))
            return

        self._device = device_index
        held, complaint = rtl_tools.acquire_sdr(self._owner, self._label, device_index)
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
            # The lease is taken before the spawn on purpose — that is what stops two
            # features opening the same dongle — so a spawn that never happened has to
            # hand it back. Without this the device reads as busy for the rest of the
            # session, with no capture running and nothing for the user to stop.
            rtl_tools.release_sdr(self._owner, device_index)
            log.exception("Could not start rtl_sdr")
            self.error.emit(f"Could not start rtl_sdr: {exc}")
            return

        # stderr is drained on its own thread for two reasons: a full pipe
        # buffer would block the tool mid-capture, and the last line it wrote
        # is the only description of what went wrong when it dies.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._proc.stderr,), daemon=True,
        )
        self._stderr_thread.start()

        self._open_recorder(center_hz, sample_rate_hz, gain_db)

        self._running = True
        self.started.emit(center_hz, sample_rate_hz, FFT_BINS)
        self._capture_loop()

    def arm_recording(self, path: Path | str | None, *, max_bytes: int | None = None) -> None:
        """Record the next capture to `path`; pass None to disarm.

        Separate from start() so the recorder's metadata can be written from the
        settings the capture actually ran with, rather than ones the caller
        hoped for.
        """
        self._record_to = Path(path) if path else None
        self._record_max_bytes = max_bytes

    def _open_recorder(
        self, center_hz: float, sample_rate_hz: float, gain_db: float
    ) -> None:
        if self._record_to is None:
            return
        try:
            self._recorder = IqRecorder(
                self._record_to,
                center_hz=center_hz,
                sample_rate_hz=sample_rate_hz,
                # A negative value means automatic gain, and the number the
                # tuner actually picked is not visible from here, so the file
                # records 0 rather than a value that was never used.
                gain_db=gain_db if gain_db >= 0 else 0.0,
                ppm=self._ppm,
                max_bytes=self._record_max_bytes,
            )
        except CaptureError as exc:
            log.warning("Could not start recording: %s", exc)
            self._recorder = None
            self.recording_failed.emit(str(exc))

    def _tee(self, raw: bytes) -> None:
        """Copy the live stream into the recording, if one is running.

        A recording failure must not stop the capture: a spectrum with a full
        disk is still worth watching, so the error is reported and recording
        ends on its own.
        """
        recorder = self._recorder
        if recorder is None:
            return
        try:
            recorder.write(raw)
            if recorder.truncated:
                self.stop_recording()
        except CaptureError as exc:
            log.warning("Recording stopped: %s", exc)
            self._recorder = None
            self.recording_failed.emit(str(exc))

    def _finish_recording(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return
        try:
            info = recorder.close()
        except CaptureError as exc:
            log.warning("Could not finish the recording: %s", exc)
            self.recording_failed.emit(str(exc))
            return
        self.recording_finished.emit(info)

    @Slot()
    def stop_recording(self) -> None:
        """End the recording but leave the capture running."""
        self._finish_recording()

    @property
    def is_recording(self) -> bool:
        return self._recorder is not None

    def _drain_stderr(self, stream: IO[bytes] | None) -> None:
        if stream is None:
            return
        try:
            for raw in stream:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    with self._stderr_lock:
                        self._stderr_lines.append(line)
        except (OSError, ValueError) as exc:
            # Happens when the pipe is closed out from under us on stop.
            log.debug("rtl_sdr stderr drain ended: %s", exc)

    def _capture_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        while self._running:
            # read() on a buffered pipe returns the full amount once enough
            # data arrives, and b'' at EOF — which is the only thing that
            # tells us rtl_sdr is gone short of polling it.
            raw = proc.stdout.read(_READ_SAMPLES * 2)
            if not raw:
                break
            self._tee(raw)
            row = iq_to_power_row(raw)
            if row is not None:
                self.row_ready.emit(row)

        # Two things make an exit normal, and both have to be checked. A
        # stop() clears `_running` before taking the process down; but rtl_sdr
        # also exits by itself with status 0 (it reports "User cancel" and
        # quits when told to stop reading), and treating that as a failure
        # would put an error on screen for a perfectly clean shutdown.
        # stderr is not a signal here either way: a normal exit still writes
        # "rtlsdr_demod_write_reg failed with -9" as the device is closed.
        intentional = not self._running
        tail, code = self._close()
        if intentional or code == 0:
            # Only emit stopped for a normal stop. When this fired
            # unconditionally, an error exit emitted BOTH error and stopped,
            # and the page's stopped handler overwrote the "Error" status with
            # "Capture stopped" — silently hiding that anything had gone wrong.
            self.stopped.emit("Capture stopped")
            return
        self.error.emit(rtl_tools.explain_failure(tail))

    @Slot()
    def stop(self) -> None:
        """Ask the capture to end. Safe to call from the GUI thread.

        This has to both clear the flag and take the process down: the worker
        is blocked inside stdout.read(), where it will never process a queued
        event, so terminate() is what actually unblocks it.
        """
        self._running = False
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                log.warning("Could not terminate rtl_sdr", exc_info=True)

    def _close(self) -> tuple[str, int | None]:
        """Tear the child process down; return (last thing it said, exit code)."""
        # Before the process teardown: the recording is finished and its sidecar
        # written whether the capture stopped cleanly or died.
        self._finish_recording()
        proc, self._proc = self._proc, None
        rtl_tools.release_sdr(self._owner, self._device)
        code: int | None = proc.poll() if proc is not None else None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    code = proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # A wedged driver is exactly the case this exists for.
                    log.warning("rtl_sdr ignored terminate(); killing it")
                    proc.kill()
                    try:
                        code = proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        log.error("rtl_sdr could not be killed")
                        code = None
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

        thread, self._stderr_thread = self._stderr_thread, None
        if thread is not None:
            thread.join(timeout=1)

        with self._stderr_lock:
            lines = list(self._stderr_lines)
            self._stderr_lines.clear()
        # The last line is the one that explains the exit; earlier lines are
        # the banner and the tuning confirmation.
        return (lines[-1] if lines else ""), code

    def shutdown(self) -> None:
        """Stop and release the process (used when the page is going away)."""
        self.stop()
        self._close()

    def stderr_tail(self) -> str:
        """Everything the tool said, for a diagnostics view."""
        with self._stderr_lock:
            return "\n".join(self._stderr_lines)


class SdrController(QObject):
    """GUI-facing facade; owns the worker thread."""

    row_ready = Signal(object)
    started = Signal(float, float, int)
    stopped = Signal(str)
    error = Signal(str)
    recording_finished = Signal(object)  # iq_recorder.CaptureInfo
    recording_failed = Signal(str)

    def __init__(self, parent=None, *, label: str = "the spectrum view"):
        super().__init__(parent)
        self._worker = SdrWorker(label=label)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        self._worker.row_ready.connect(self.row_ready)
        self._worker.started.connect(self.started)
        self._worker.stopped.connect(self.stopped)
        self._worker.error.connect(self.error)
        self._worker.recording_finished.connect(self.recording_finished)
        self._worker.recording_failed.connect(self.recording_failed)

        self._thread.start()

    def start(
        self,
        center_hz: float,
        sample_rate_hz: float,
        gain_db: float,
        device_index: int = 0,
    ) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(
            self._worker, "start", Qt.ConnectionType.QueuedConnection,
            Q_ARG(float, center_hz), Q_ARG(float, sample_rate_hz), Q_ARG(float, gain_db),
            # Q_ARG(int, ...) is safe where Q_ARG(object, ...) is not: an int has a
            # registered meta type to marshal, and a Python object does not.
            Q_ARG(int, int(device_index)),
        )

    def stop(self) -> None:
        # Direct call (not queued): the worker is inside its blocking capture
        # loop, so a queued invocation would not be processed until it exits.
        self._worker.stop()

    def arm_recording(self, path, *, max_bytes: int | None = None) -> None:
        """Record the next capture to `path`.

        Called directly like stop(): the worker only reads this when it starts,
        so there is nothing to race with and a queued call would only add a
        chance of the two arriving out of order.
        """
        self._worker.arm_recording(path, max_bytes=max_bytes)

    def stop_recording(self) -> None:
        """End recording but leave the capture running. Direct, like stop()."""
        self._worker.stop_recording()

    def shutdown(self) -> None:
        self.stop()
        self._thread.quit()
        self._thread.wait(3000)
