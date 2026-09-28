"""Tests for the band accumulator that fills rtl_power's per-sweep holes.

rtl_power loses a scattered ~2.6% of bins every sweep and cannot be flag-fixed
(measured: 30% at -i 5, 48% in single-shot mode). The holes differ every sweep,
which is what makes them recoverable. These tests drive synthetic sweeps with
known hole patterns — no dongle, no process.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshchat.services.rtl_scan import BandAccumulator, PowerRow

_LOW = 902_000_000.0
_STEP = 81_250.0


def _sweep(values, *, low=_LOW, step=_STEP) -> PowerRow:
    """A sweep over `values`, where None means a bin rtl_power could not measure."""
    power = np.array(
        [np.nan if value is None else float(value) for value in values], dtype=np.float32
    )
    count = power.size
    return PowerRow(low, low + (count - 1) * step, step, 256, power)


class TestFirstSweep:
    def test_it_reports_the_coverage_it_actually_has(self):
        accumulator = BandAccumulator()

        picture = accumulator.update(_sweep([1.0, None, 3.0, None]))

        assert picture.coverage == pytest.approx(0.5)
        assert picture.sweeps == 1
        assert np.isnan(picture.row.power_db[1])

    def test_holes_start_out_marked_never_measured(self):
        accumulator = BandAccumulator()

        picture = accumulator.update(_sweep([1.0, None, 3.0]))

        assert picture.ages[1] == -1, "never measured is not the same as old"
        assert picture.ages[0] == 0

    def test_a_complete_sweep_is_already_full_coverage(self):
        accumulator = BandAccumulator()

        picture = accumulator.update(_sweep([1.0, 2.0, 3.0]))

        assert picture.coverage == pytest.approx(1.0)


class TestFillingAcrossSweeps:
    def test_complementary_holes_become_a_complete_band(self):
        """The whole point: two lossy sweeps can add up to a whole picture."""
        accumulator = BandAccumulator()

        accumulator.update(_sweep([1.0, None, None, 4.0]))
        picture = accumulator.update(_sweep([None, 2.0, 3.0, None]))

        assert picture.coverage == pytest.approx(1.0)
        assert picture.row.power_db.tolist() == pytest.approx([1.0, 2.0, 3.0, 4.0])

    def test_a_remembered_bin_keeps_its_value_when_the_next_sweep_misses_it(self):
        accumulator = BandAccumulator()
        accumulator.update(_sweep([7.5, None]))

        picture = accumulator.update(_sweep([None, None]))

        assert picture.row.power_db[0] == pytest.approx(7.5), "held, not dropped"

    def test_a_re_measured_bin_takes_the_new_value(self):
        accumulator = BandAccumulator()
        accumulator.update(_sweep([1.0, 1.0]))

        picture = accumulator.update(_sweep([9.0, None]))

        assert picture.row.power_db[0] == pytest.approx(9.0), "fresh data wins"

    def test_age_counts_sweeps_since_the_bin_was_last_seen(self):
        accumulator = BandAccumulator()
        accumulator.update(_sweep([1.0, 1.0]))

        picture = accumulator.update(_sweep([1.0, None]))
        assert picture.ages[1] == 1

        picture = accumulator.update(_sweep([1.0, None]))
        assert picture.ages[1] == 2, "so a caller can draw a stale bin differently"
        assert picture.ages[0] == 0

    def test_an_unmeasured_bin_does_not_age(self):
        """-1 must not creep upward and eventually look like a real old reading."""
        accumulator = BandAccumulator()

        for _ in range(5):
            picture = accumulator.update(_sweep([1.0, None]))

        assert picture.ages[1] == -1


class TestStaleness:
    def test_a_bin_unseen_for_too_long_reverts_to_unknown(self):
        """Holding a value forever would present a stale reading as current."""
        accumulator = BandAccumulator(max_age=3)
        accumulator.update(_sweep([5.0, 5.0]))

        for _ in range(4):
            picture = accumulator.update(_sweep([5.0, None]))

        assert np.isnan(picture.row.power_db[1]), "gone stale, so no longer shown"
        assert picture.coverage == pytest.approx(0.5)

    def test_a_bin_seen_again_comes_back(self):
        accumulator = BandAccumulator(max_age=3)
        accumulator.update(_sweep([5.0, 5.0]))
        for _ in range(4):
            accumulator.update(_sweep([5.0, None]))

        picture = accumulator.update(_sweep([5.0, 8.0]))

        assert picture.row.power_db[1] == pytest.approx(8.0)
        assert picture.ages[1] == 0


class TestGeometryChanges:
    def test_a_different_span_starts_a_new_picture(self):
        """Merging two spans would place power where it was never measured."""
        accumulator = BandAccumulator()
        accumulator.update(_sweep([1.0, 2.0]))

        picture = accumulator.update(_sweep([3.0, None], low=915_000_000.0))

        assert picture.row.low_hz == 915_000_000.0
        assert picture.coverage == pytest.approx(0.5), "the old sweep must not carry over"
        assert picture.sweeps == 1

    def test_a_different_bin_count_starts_a_new_picture(self):
        accumulator = BandAccumulator()
        accumulator.update(_sweep([1.0, 2.0, 3.0]))

        picture = accumulator.update(_sweep([1.0, 2.0]))

        assert picture.row.bin_count == 2
        assert picture.sweeps == 1

    def test_an_identical_geometry_continues_the_picture(self):
        accumulator = BandAccumulator()
        accumulator.update(_sweep([1.0, 2.0]))

        picture = accumulator.update(_sweep([1.0, 2.0]))

        assert picture.sweeps == 2

    def test_the_reported_geometry_is_preserved(self):
        accumulator = BandAccumulator()

        picture = accumulator.update(_sweep([1.0, 2.0]))

        assert picture.row.low_hz == _LOW
        assert picture.row.step_hz == _STEP
        assert picture.row.high_hz == _LOW + _STEP

    def test_an_emitted_picture_does_not_change_underneath_the_holder(self):
        """Each picture must be a snapshot, not a view of the live buffer.

        The accumulator mutates its arrays in place, so handing them out by
        reference let a later sweep rewrite an already-emitted picture. A
        redraw would then show the newest sweep under an older one's caption,
        and both the coverage figure and the readable history would be wrong.
        """
        accumulator = BandAccumulator()

        first = accumulator.update(_sweep([1.0, None]))
        coverage_before = first.coverage
        ages_before = first.ages.copy()

        for _ in range(4):
            accumulator.update(_sweep([None, 9.0]))

        assert first.coverage == pytest.approx(coverage_before)
        assert np.isnan(first.row.power_db[1]), "the first sweep never measured bin 1"
        assert first.ages.tolist() == ages_before.tolist()

    def test_a_retained_picture_still_shows_its_own_sweep_number(self):
        accumulator = BandAccumulator()

        first = accumulator.update(_sweep([1.0, 2.0]))
        accumulator.update(_sweep([1.0, 2.0]))

        assert first.sweeps == 1


class TestRealisticLoss:
    def test_a_few_noisy_sweeps_reach_full_coverage(self):
        """The actual behaviour: 97% per sweep, scattered, different each time.

        Modelled on the measured 2.6% loss over a 321-bin band.
        """
        accumulator = BandAccumulator()
        rng = np.random.default_rng(20260927)
        bins = 321
        truth = rng.normal(0.0, 1.0, bins)

        first = accumulator.update(self._lossy(truth, rng, 0.026))
        assert first.coverage < 0.99, "one sweep really does have holes"

        for _ in range(4):
            picture = accumulator.update(self._lossy(truth, rng, 0.026))

        assert picture.coverage == pytest.approx(1.0), "holes are filled within seconds"

    @staticmethod
    def _lossy(truth: np.ndarray, rng, rate: float) -> PowerRow:
        values = truth.copy()
        holes = rng.random(values.size) < rate
        values[holes] = np.nan
        return PowerRow(_LOW, _LOW + (values.size - 1) * _STEP, _STEP, 256, values.astype(np.float32))
