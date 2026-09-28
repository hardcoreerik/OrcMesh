"""Tests for the statistics behind the analysis modes.

These check the properties each mode depends on, not just that arrays come back: that peak hold
remembers a signal that has gone, that occupancy counts time rather than strength, that a burst
is one event and not one per contiguous run, and that the floor is robust to a carrier sitting
inside the row.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshchat.analytics import spectral_history as sh

BINS = 64


def _quiet(bins: int = BINS, *, floor_db: float = -100.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.full(bins, floor_db, dtype=np.float64) + rng.normal(0, 0.5, bins)


def _with_tone(bins: int = BINS, *, index: int = 32, level_db: float = -60.0,
               floor_db: float = -100.0, width: int = 1, seed: int = 1) -> np.ndarray:
    row = _quiet(bins, floor_db=floor_db, seed=seed)
    row[max(0, index - width // 2): index + width // 2 + 1] = level_db
    return row


class TestEstimateFloor:
    def test_the_median_ignores_a_carrier_in_the_row(self):
        """The reason it is a median.

        A mean is dragged up by whatever strong signal happens to be present, so using one
        would make the floor move with the traffic and every threshold built on it drift.
        """
        row = _with_tone(level_db=-40.0, width=4)

        assert sh.estimate_floor(row) == pytest.approx(-100.0, abs=1.0)

    def test_the_mean_would_have_been_wrong(self):
        """Stated as its own test so the choice is visible if anyone changes it."""
        row = _with_tone(level_db=-30.0, width=6)

        assert np.mean(row) > sh.estimate_floor(row) + 5

    def test_an_all_nan_row_gives_nan_rather_than_a_number(self):
        assert np.isnan(sh.estimate_floor(np.full(BINS, np.nan)))

    def test_a_row_with_some_nan_uses_the_finite_bins(self):
        row = _quiet()
        row[:8] = np.nan

        assert sh.estimate_floor(row) == pytest.approx(-100.0, abs=1.0)


class TestSpectralHistory:
    def test_peak_hold_remembers_a_signal_that_has_gone(self):
        """The property the whole mode exists for.

        An intermittent emitter is invisible on a live spectrum most of the time. This is what
        makes it visible anyway.
        """
        history = sh.SpectralHistory(BINS, capacity=10)
        history.add(_with_tone(index=20, level_db=-50.0))
        for _ in range(5):
            history.add(_quiet())

        peak = history.peak_hold()

        assert peak[20] == pytest.approx(-50.0)

    def test_peak_hold_keeps_the_strongest_not_the_latest(self):
        history = sh.SpectralHistory(BINS, capacity=10)
        history.add(_with_tone(index=5, level_db=-70.0))
        history.add(_with_tone(index=5, level_db=-40.0))
        history.add(_with_tone(index=5, level_db=-60.0))

        assert history.peak_hold()[5] == pytest.approx(-40.0)

    def test_min_hold_tracks_the_quietest(self):
        history = sh.SpectralHistory(BINS, capacity=10)
        history.add(_with_tone(index=5, level_db=-40.0))
        history.add(_quiet())

        assert history.min_hold()[5] == pytest.approx(-100.0, abs=1.0)

    def test_a_row_of_the_wrong_length_is_refused_rather_than_broadcast(self):
        """A wrong-shaped row would otherwise poison every statistic silently."""
        history = sh.SpectralHistory(BINS)

        assert history.add(np.zeros(BINS + 1)) is False
        assert history.rejected == 1
        assert history.rows == 0

    def test_a_refused_row_leaves_the_statistics_untouched(self):
        history = sh.SpectralHistory(BINS)
        history.add(_with_tone(index=5, level_db=-50.0))
        before = history.peak_hold()

        history.add(np.zeros(3))

        assert np.array_equal(history.peak_hold(), before)

    def test_the_window_is_bounded_and_the_run_is_not(self):
        """Two different questions, kept apart on purpose."""
        history = sh.SpectralHistory(BINS, capacity=4)
        for _ in range(10):
            history.add(_quiet())

        assert history.rows == 4, "the window is capped"
        assert history.observed == 10, "but the run is still counted"

    def test_clearing_forgets_everything(self):
        history = sh.SpectralHistory(BINS)
        history.add(_with_tone(index=5, level_db=-50.0))

        history.clear()

        assert history.rows == 0 and history.observed == 0
        assert np.all(np.isneginf(history.peak_hold()))

    def test_no_data_yet_is_reported_as_such(self):
        history = sh.SpectralHistory(BINS)

        assert not history.has_data()
        assert history.rows == 0

    def test_a_zero_bin_history_is_refused(self):
        with pytest.raises(ValueError, match="at least one bin"):
            sh.SpectralHistory(0)

    def test_a_zero_capacity_history_is_refused(self):
        with pytest.raises(ValueError, match="at least one row"):
            sh.SpectralHistory(BINS, capacity=0)


class TestOccupancy:
    def test_a_constant_carrier_is_occupied_one_hundred_percent(self):
        history = sh.SpectralHistory(BINS)
        for _ in range(10):
            history.add(_with_tone(index=33, level_db=-70.0))

        assert history.occupancy()[33] == pytest.approx(1.0)

    def test_a_carrier_present_half_the_time_reads_half(self):
        history = sh.SpectralHistory(BINS)
        for _ in range(6):
            history.add(_with_tone(index=33, level_db=-70.0))
        for _ in range(6):
            history.add(_quiet())

        assert history.occupancy()[33] == pytest.approx(0.5, abs=0.01)

    def test_occupancy_separates_a_weak_constant_signal_from_a_strong_rare_one(self):
        """The distinction peak hold cannot make.

        Both produce the same *strongest* bin ever seen and completely different duty cycles,
        which is exactly the pair of facts needed to tell them apart.
        """
        constant = sh.SpectralHistory(BINS)
        for _ in range(10):
            constant.add(_with_tone(index=10, level_db=-75.0))

        rare = sh.SpectralHistory(BINS)
        rare.add(_with_tone(index=10, level_db=-20.0))
        for _ in range(9):
            rare.add(_quiet())

        assert constant.occupancy()[10] > 0.9
        assert rare.occupancy()[10] < 0.2

    def test_a_bin_that_is_never_hot_reads_zero(self):
        history = sh.SpectralHistory(BINS)
        for _ in range(5):
            history.add(_quiet())

        assert history.occupancy()[0] == 0.0

    def test_occupancy_survives_rows_leaving_the_window(self):
        """It counts the run, not the window, so a duty cycle does not jitter as rows age out."""
        history = sh.SpectralHistory(BINS, capacity=2)
        for _ in range(10):
            history.add(_with_tone(index=7, level_db=-70.0))

        assert history.occupancy()[7] == pytest.approx(1.0)

    def test_no_rows_means_no_occupancy_rather_than_a_division(self):
        assert np.all(sh.SpectralHistory(BINS).occupancy() == 0.0)


class TestPercentiles:
    def test_the_envelope_brackets_the_typical_value(self):
        history = sh.SpectralHistory(BINS)
        rng = np.random.default_rng(3)
        for _ in range(50):
            history.add(-100.0 + rng.normal(0, 2, BINS))

        bands = history.percentiles()

        assert set(bands) == {5.0, 50.0, 95.0}
        assert np.all(bands[5.0] <= bands[50.0])
        assert np.all(bands[50.0] <= bands[95.0])

    def test_a_percentile_that_never_varies_is_not_a_distribution(self):
        """A constant bin has all three percentiles equal, which is the honest answer."""
        history = sh.SpectralHistory(BINS)
        for _ in range(10):
            history.add(_quiet())

        bands = history.percentiles()

        assert bands[5.0][0] == pytest.approx(bands[95.0][0], abs=1.5)

    def test_no_rows_gives_nan_not_zero(self):
        """Zero dB is a real signal level; unknown is not, and must not be drawn as one."""
        bands = sh.SpectralHistory(BINS).percentiles()

        assert all(np.all(np.isnan(band)) for band in bands.values())


class TestBurstDetector:
    def test_one_burst_is_one_event(self):
        detector = sh.BurstDetector(BINS)
        events: list[sh.Burst] = []
        for _ in range(3):
            events += detector.push(_with_tone(index=10, level_db=-50.0))
        events += detector.push(_quiet())

        assert len(events) == 1
        assert events[0].row_span == 3

    def test_a_notched_spectrum_is_still_one_event(self):
        """A modulated signal has gaps in its spectrum; those are not event boundaries."""
        detector = sh.BurstDetector(BINS)
        row = _quiet()
        row[10:12] = -50.0
        row[20:22] = -50.0  # a second lobe, well separated
        detector.push(row)
        events = detector.flush()

        assert len(events) == 1
        assert events[0].first_bin == 10
        assert events[0].last_bin == 21

    def test_two_separate_bursts_are_two_events(self):
        detector = sh.BurstDetector(BINS)
        events: list[sh.Burst] = []
        events += detector.push(_with_tone(index=10, level_db=-50.0))
        events += detector.push(_quiet())
        events += detector.push(_with_tone(index=40, level_db=-50.0))
        events += detector.push(_quiet())

        assert len(events) == 2
        assert events[0].first_bin < events[1].first_bin

    def test_a_single_row_spike_is_an_event_by_default(self):
        detector = sh.BurstDetector(BINS)
        detector.push(_with_tone(index=10, level_db=-50.0))

        assert len(detector.flush()) == 1

    def test_a_minimum_duration_filters_single_row_spikes(self):
        """The knob for ignoring one-row glitches, which a real capture is full of."""
        detector = sh.BurstDetector(BINS, min_rows=3)
        detector.push(_with_tone(index=10, level_db=-50.0))

        assert detector.flush() == []

    def test_an_event_still_running_is_reported_by_flush(self):
        detector = sh.BurstDetector(BINS)
        detector.push(_with_tone(index=10, level_db=-50.0))

        assert detector.in_burst
        assert len(detector.flush()) == 1
        assert not detector.in_burst

    def test_flushing_twice_does_not_report_twice(self):
        detector = sh.BurstDetector(BINS)
        detector.push(_with_tone(index=10, level_db=-50.0))

        assert len(detector.flush()) == 1
        assert detector.flush() == []

    def test_quiet_rows_produce_nothing(self):
        detector = sh.BurstDetector(BINS)
        events: list[sh.Burst] = []
        for _ in range(5):
            events += detector.push(_quiet())

        assert events == []
        assert not detector.in_burst

    def test_the_event_describes_itself_in_usable_units(self):
        """Two rows of three bins, at the scales the display knows."""
        detector = sh.BurstDetector(BINS)
        detector.push(_with_tone(index=32, level_db=-50.0, width=3))
        detector.push(_with_tone(index=32, level_db=-50.0, width=3))
        [event] = detector.push(_quiet())

        text = event.describe(bin_hz=1_000.0, row_s=0.02)

        assert "kHz wide" in text
        assert "over floor" in text
        assert event.row_span == 2
        assert event.bin_span == 3

    def test_a_burst_reports_how_far_over_the_floor_it_was(self):
        detector = sh.BurstDetector(BINS)
        detector.push(_with_tone(index=10, level_db=-50.0, floor_db=-100.0))
        [event] = detector.push(_quiet(floor_db=-100.0))

        assert event.over_floor_db == pytest.approx(50.0, abs=2)

    def test_a_wrong_shaped_row_is_ignored(self):
        detector = sh.BurstDetector(BINS)

        assert detector.push(np.zeros(3)) == []
        assert detector.rows_seen == 0


class TestSlotGrid:
    def rows(self, count: int, *, span_bins: int = BINS) -> list[np.ndarray]:
        return [_quiet(span_bins) for _ in range(count)]

    def test_the_grid_is_slots_by_time(self):
        rows = self.rows(5)

        grid, centres = sh.slot_power_grid(
            rows, centre_hz=906_875_000.0, span_hz=2_000_000.0, slot_width_hz=250_000.0,
        )

        assert grid.shape[1] == 5, "one column per row"
        assert grid.shape[0] == len(centres) == 8, "2 MHz of 250 kHz slots"

    def test_the_slot_centres_are_where_the_slots_are(self):
        _, centres = sh.slot_power_grid(
            self.rows(1), centre_hz=906_000_000.0, span_hz=1_000_000.0, slot_width_hz=250_000.0,
        )

        assert centres[0] == pytest.approx(905_625_000.0)
        assert centres[-1] == pytest.approx(906_375_000.0)

    def test_a_tone_lands_in_the_slot_that_contains_it(self):
        """The property that makes the view a statement about the receiver's channel plan."""
        row = _quiet()
        row[32] = -40.0  # the centre bin
        grid, centres = sh.slot_power_grid(
            [row], centre_hz=906_875_000.0, span_hz=2_000_000.0, slot_width_hz=250_000.0,
        )

        loudest = int(np.nanargmax(grid[:, 0]))
        assert centres[loudest] == pytest.approx(906_875_000.0, abs=125_001)

    def test_the_number_of_slots_is_capped(self):
        grid, centres = sh.slot_power_grid(
            self.rows(1), centre_hz=900e6, span_hz=900_000_000.0,
            slot_width_hz=250_000.0, max_slots=16,
        )

        assert grid.shape[0] == 16 == len(centres)

    def test_no_rows_is_an_empty_grid_not_an_error(self):
        grid, centres = sh.slot_power_grid([], centre_hz=900e6, span_hz=1e6)

        assert grid.size == 0 and centres.size == 0

    def test_a_span_of_zero_is_an_empty_grid(self):
        grid, centres = sh.slot_power_grid(self.rows(2), centre_hz=900e6, span_hz=0.0)

        assert grid.size == 0 and centres.size == 0

    def test_a_slot_nobody_measured_reads_as_unknown_not_as_zero(self):
        """A survey leaves holes, and a hole is not a quiet channel.

        Zero dB is a real signal level; a slot with no measured bins has no level at all. The
        first slot here is entirely NaN because that is exactly what was fed in.
        """
        row = _quiet()
        row[:8] = np.nan

        grid, _ = sh.slot_power_grid(
            [row], centre_hz=0.0, span_hz=float(BINS), slot_width_hz=8.0,
        )

        assert np.isnan(grid[0, 0]), "never measured"
        assert np.isfinite(grid[1:, 0]).all(), "measured slots are real"

    def test_a_slot_with_one_measured_bin_is_still_measured(self):
        """One good bin in a slot is a measurement, not a hole."""
        row = _quiet()
        row[:7] = np.nan  # only bin 7 of the first 8 survives

        grid, _ = sh.slot_power_grid(
            [row], centre_hz=0.0, span_hz=float(BINS), slot_width_hz=8.0,
        )

        assert np.isfinite(grid[0, 0])
        assert grid[0, 0] == pytest.approx(-100.0, abs=2.0)
