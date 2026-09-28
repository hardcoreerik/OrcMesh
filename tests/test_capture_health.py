"""Tests for the capture's account of its own condition.

The brief asked for a display that can say "1.4 s behind, 312 frames dropped".
Nothing could say either, because `row_ready` is an unbounded queued signal and
nothing was counting. These tests pin the arithmetic and, more importantly, the
vocabulary: the figures must not claim to distinguish loss from queued data, which
they cannot do from outside the driver.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshchat.services import sdr_source
from meshchat.services.sdr_source import CaptureHealth, measure_capture_health

RATE = 2_400_000.0


def _iq_bytes(samples) -> bytes:
    samples = np.asarray(samples)
    i = np.clip(np.round(samples.real * 127.5 + 127.5), 0, 255).astype(np.uint8)
    q = np.clip(np.round(samples.imag * 127.5 + 127.5), 0, 255).astype(np.uint8)
    return np.stack((i, q), axis=1).tobytes()


class _Stdout:
    """One chunk then EOF — the least a capture needs to say anything about itself."""

    closed = False

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._sent = False

    def read(self, count: int) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._payload

    def close(self) -> None:
        self.closed = True


class _Process:
    """A Popen double that has already exited."""

    returncode = 0
    pid = 4244

    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdout = _Stdout(stdout)
        self.stderr = _Stdout(stderr)

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


def _health(elapsed_s: float, received_samples: int, rows: int = 0) -> CaptureHealth:
    return measure_capture_health(
        received_bytes=received_samples * 2,
        sample_rate_hz=RATE,
        elapsed_s=elapsed_s,
        rows=rows,
    )


class TestArithmetic:
    def test_a_capture_that_is_caught_up_is_not_behind(self):
        """Ten seconds at the rate asked for is ten seconds of samples, no lag."""
        health = _health(10.0, int(10.0 * RATE))

        assert health.lag_s == 0.0
        assert health.shortfall_samples == 0
        assert health.shortfall_percent == 0.0
        assert health.is_keeping_up

    def test_falling_behind_is_reported_in_both_seconds_and_samples(self):
        """The two figures are one measurement in two units, and are both kept.

        A second of missing samples at 2.4 MSPS is 2.4 M samples, and the same
        shortfall is a second of lag. Saying so is the point: the display needs the
        seconds to say how stale it is and the samples to say how much is not in hand.
        """
        health = _health(10.0, int(9.0 * RATE))

        assert health.lag_s == pytest.approx(1.0)
        assert health.shortfall_samples == int(RATE)
        assert health.shortfall_percent == pytest.approx(10.0)
        assert not health.is_keeping_up

    def test_the_effective_rate_is_what_arrived_not_what_was_asked_for(self):
        health = _health(10.0, int(9.0 * RATE))

        assert health.effective_rate_hz == pytest.approx(0.9 * RATE)

    def test_a_half_complex_sample_is_discarded_rather_than_counted(self):
        """An odd byte count cannot be a sample, and must not become half of one."""
        health = measure_capture_health(
            received_bytes=101, sample_rate_hz=RATE, elapsed_s=1.0, rows=0,
        )

        assert health.received_samples == 50

    def test_a_negative_lag_is_clamped_away(self):
        """No hardware can be ahead of the clock; a negative lag means we misread it."""
        health = measure_capture_health(
            received_bytes=int(20.0 * RATE) * 2, sample_rate_hz=RATE, elapsed_s=10.0, rows=0,
        )

        assert health.lag_s == 0.0

    def test_an_instant_after_the_first_byte_does_not_divide_by_zero(self):
        health = _health(0.0, 0)

        assert health.effective_rate_hz == 0.0
        assert health.shortfall_percent == 0.0

    def test_a_zero_rate_does_not_divide_by_zero(self):
        """An unknown rate suppresses the figures that need it, and only those.

        Lag and shortfall both need a rate to say anything — with none, the honest
        answer is nothing rather than an invented number, since a capture whose rate
        is not yet known would otherwise read as permanently behind. The effective
        rate needs no such thing: 512 samples in one second is 512 samples per second
        whatever was asked for, which is exactly why it is the figure that stays
        meaningful when the requested rate turns out to be wrong.
        """
        health = measure_capture_health(
            received_bytes=1024, sample_rate_hz=0.0, elapsed_s=1.0, rows=0,
        )

        assert health.lag_s == 0.0, "no rate means no lag, not an invented one"
        assert health.shortfall_samples == 0, "nothing is expected at a zero rate"
        assert health.effective_rate_hz == pytest.approx(512.0)

    def test_a_capture_with_no_samples_is_all_shortfall_not_negative(self):
        health = _health(5.0, 0)

        assert health.shortfall_samples == int(5.0 * RATE)
        assert health.shortfall_percent == pytest.approx(100.0)


class TestKeepingUpBoundary:
    """The tolerance is a boundary, so both sides of it are pinned."""

    def test_exactly_at_the_tolerance_still_counts_as_live(self):
        lag = sdr_source.LAG_TOLERANCE_S
        health = _health(10.0, int((10.0 - lag) * RATE))

        assert health.is_keeping_up

    def test_just_past_the_tolerance_does_not(self):
        lag = sdr_source.LAG_TOLERANCE_S * 2
        health = _health(10.0, int((10.0 - lag) * RATE))

        assert not health.is_keeping_up


class TestDescription:
    def test_a_healthy_capture_is_described_without_alarm(self):
        health = _health(10.0, int(10.0 * RATE), rows=729)

        text = health.describe()

        assert "keeping up" in text
        assert "2.40 MS/s" in text
        assert "729" in text

    def test_a_lagging_capture_leads_with_the_lag(self):
        health = _health(10.0, int(8.6 * RATE), rows=600)

        text = health.describe()

        assert "1400 ms behind" in text
        assert "samples short" in text
        assert "14.0%" in text

    def test_a_slight_shortfall_is_not_called_healthy_even_if_the_lag_is_small(self):
        """Under 1% of a long capture is still hundreds of thousands of samples."""
        health = _health(100.0, int(99.5 * RATE))

        assert "short" in health.describe()

    def test_the_word_lossless_is_never_used(self):
        """The vocabulary guard.

        Nothing here can prove the absence of a driver drop inside a buffer, so
        "lossless" would be a claim the arithmetic does not support. If a future
        edit reaches for the better-sounding word, this fails.
        """
        texts = [
            _health(10.0, int(10.0 * RATE)).describe(),
            _health(10.0, int(9.0 * RATE)).describe(),
            _health(1.0, 0).describe(),
        ]

        assert not any("lossless" in text for text in texts), (
            "a capture that never fell behind has not demonstrated it lost nothing"
        )

    def test_every_description_is_a_single_line(self):
        """It goes in a status label, which does not wrap."""
        for health in (_health(10.0, int(10.0 * RATE)), _health(10.0, int(9.0 * RATE))):
            assert "\n" not in health.describe()


class TestWorkerWiring:
    """The counters have to be fed by the loop, not merely exist."""

    def test_the_final_figure_is_reported_after_the_capture_ends(self):
        worker = sdr_source.SdrWorker()
        payload = _iq_bytes(np.zeros(sdr_source.FFT_BINS * 2, dtype=np.complex64))
        worker._proc = _Process(payload)
        worker._running = True
        worker._reset_health(RATE)

        reports: list[CaptureHealth] = []
        worker.health.connect(reports.append)

        worker._capture_loop()

        assert len(reports) == 1, "one figure, and it is the one describing how it went"
        assert reports[0].received_samples == sdr_source.FFT_BINS * 2
        assert reports[0].rows == 1

    def test_a_capture_that_never_delivered_a_byte_reports_nothing(self):
        worker = sdr_source.SdrWorker()
        worker._reset_health(RATE)

        assert worker.capture_health() is None

    def test_no_figure_is_emitted_before_any_data_arrives(self):
        worker = sdr_source.SdrWorker()
        worker._reset_health(RATE)
        reports: list[CaptureHealth] = []
        worker.health.connect(reports.append)

        worker._report_health(force=True)

        assert reports == []

    def test_the_health_is_readable_without_waiting_for_a_signal(self):
        """The status bar polls; it must not have to wait a second for a signal."""
        worker = sdr_source.SdrWorker()
        worker._reset_health(RATE)
        worker._note_progress(2048)

        health = worker.capture_health()

        assert health is not None
        assert health.received_samples == 1024

    def test_note_progress_counts_bytes_across_calls(self):
        worker = sdr_source.SdrWorker()
        worker._reset_health(RATE)
        worker._note_progress(1024)
        worker._note_progress(1024)

        health = worker.capture_health()

        assert health is not None
        assert health.received_samples == 1024

    def test_resetting_health_starts_the_counters_over(self):
        """A second capture must not inherit the first one's shortfall."""
        worker = sdr_source.SdrWorker()
        worker._reset_health(RATE)
        worker._note_progress(4096)

        worker._reset_health(RATE)

        assert worker.capture_health() is None, "no data yet for the new capture"

    def test_the_payload_used_above_really_does_produce_a_row(self):
        """Guards the wiring test itself: a short payload would yield no row."""
        payload = _iq_bytes(np.zeros(sdr_source.FFT_BINS * 2, dtype=np.complex64))

        assert sdr_source.iq_to_power_row(payload) is not None
