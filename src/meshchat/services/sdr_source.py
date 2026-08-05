"""MeshChat – RTL-SDR capture source for the spectrum waterfall.

Runs the blocking SDR read loop on its own QThread and emits FFT power rows.
Requires the `pyrtlsdr` package AND the native librtlsdr driver (on Windows,
that means installing the WinUSB driver for the dongle via Zadig). Both are
optional — MeshChat runs normally without them, the Spectrum page just shows
an unavailable state.
"""
from __future__ import annotations

import logging

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Slot

log = logging.getLogger(__name__)

FFT_BINS = 1024
_SAMPLES_PER_READ = FFT_BINS * 16


def sdr_available() -> tuple[bool, str]:
    """Return (available, reason). Checks the Python binding and the native
    driver separately, since they fail for different, user-fixable reasons."""
    try:
        import rtlsdr  # noqa: F401
    except ImportError:
        return False, (
            "The pyrtlsdr package is not installed.\n\n"
            "Install it with:  pip install pyrtlsdr"
        )
    try:
        from rtlsdr import RtlSdr
        count = RtlSdr.get_device_count()
    except Exception as exc:
        return False, (
            "The native librtlsdr driver could not be loaded.\n\n"
            "On Windows, install the WinUSB driver for your dongle using Zadig, "
            "and make sure librtlsdr.dll is on PATH.\n\n"
            f"Details: {exc}"
        )
    if count == 0:
        return False, "No RTL-SDR device detected. Plug the dongle in and try again."
    return True, f"{count} RTL-SDR device(s) detected."


class SdrWorker(QObject):
    """Blocking SDR reads live here, never on the GUI thread."""

    row_ready = Signal(object)          # np.ndarray of FFT power in dB
    started = Signal(float, float, int)  # center_hz, span_hz, bins
    stopped = Signal(str)                # reason / status message
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sdr = None
        self._running = False

    @Slot(float, float, float)
    def start(self, center_hz: float, sample_rate_hz: float, gain_db: float) -> None:
        if self._running:
            return
        try:
            from rtlsdr import RtlSdr
            self._sdr = RtlSdr()
            self._sdr.sample_rate = sample_rate_hz
            self._sdr.center_freq = center_hz
            # 'auto' is a valid librtlsdr gain mode; a float sets manual gain
            self._sdr.gain = "auto" if gain_db < 0 else gain_db
        except Exception as exc:
            log.exception("SDR start failed")
            self._close()
            self.error.emit(f"Could not start the RTL-SDR: {exc}")
            return

        self._running = True
        self.started.emit(center_hz, sample_rate_hz, FFT_BINS)
        self._capture_loop()

    def _capture_loop(self) -> None:
        window = np.hanning(FFT_BINS)
        while self._running and self._sdr is not None:
            try:
                samples = self._sdr.read_samples(_SAMPLES_PER_READ)
            except Exception as exc:
                log.exception("SDR read failed")
                self.error.emit(f"SDR read failed: {exc}")
                break

            # Average several FFTs per displayed row to smooth the noise floor
            chunks = len(samples) // FFT_BINS
            if chunks == 0:
                continue
            acc = np.zeros(FFT_BINS, dtype=np.float64)
            for i in range(chunks):
                chunk = samples[i * FFT_BINS:(i + 1) * FFT_BINS] * window
                spectrum = np.fft.fftshift(np.fft.fft(chunk))
                acc += np.abs(spectrum) ** 2
            power = acc / chunks
            power_db = 10.0 * np.log10(power + 1e-12)
            self.row_ready.emit(power_db.astype(np.float32))

        self._close()
        self.stopped.emit("Capture stopped")

    @Slot()
    def stop(self) -> None:
        self._running = False

    def _close(self) -> None:
        if self._sdr is not None:
            try:
                self._sdr.close()
            except Exception as exc:
                log.warning("Closing the RTL-SDR failed: %s", exc)
            self._sdr = None
        self._running = False


class SdrController(QObject):
    """GUI-facing facade; owns the worker thread."""

    row_ready = Signal(object)
    started = Signal(float, float, int)
    stopped = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = SdrWorker()
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        self._worker.row_ready.connect(self.row_ready)
        self._worker.started.connect(self.started)
        self._worker.stopped.connect(self.stopped)
        self._worker.error.connect(self.error)

        self._thread.start()

    def start(self, center_hz: float, sample_rate_hz: float, gain_db: float) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(
            self._worker, "start", Qt.ConnectionType.QueuedConnection,
            Q_ARG(float, center_hz), Q_ARG(float, sample_rate_hz), Q_ARG(float, gain_db),
        )

    def stop(self) -> None:
        # Direct call (not queued): the worker is inside its blocking capture
        # loop, so a queued invocation would not be processed until it exits.
        self._worker.stop()

    def shutdown(self) -> None:
        self.stop()
        self._thread.quit()
        self._thread.wait(3000)
