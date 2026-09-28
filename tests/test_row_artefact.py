"""Tests for the rtl_power row-artefact correction.

The artefact is not theoretical: measured on this bench over 902-928 MHz at 81.25 kHz it is
exactly three bins at +4.33 .. +5.89 dB above the row's neighbours, in **200 of 200** rows, and
in a quiet survey it is the brightest thing present. These tests pin both halves of the fix —
that it removes the artefact, and that it refuses to remove anything that might be real.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshchat.services.rtl_scan import (
    ROW_ARTEFACT_GUARD_DB,
    ROW_ARTEFACT_MIN_DB,
    PowerRow,
    merge_rows,
    repair_row_artefact,
)


def _row(bins: int = 33, *, floor_db: float = -100.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (np.full(bins, floor_db) + rng.normal(0, 0.3, bins)).astype(np.float32)


def _with_artefact(bins: int = 33, *, bump_db: float = 5.19, seed: int = 0) -> np.ndarray:
    """A row with the measured artefact: three bins at the centre, that far over the rest."""
    values = _row(bins, seed=seed)
    centre = bins // 2
    values[centre - 1:centre + 2] += bump_db
    return values


class TestRemoval:
    def test_the_measured_artefact_is_removed(self):
        """Three bins, +5.19 dB — the median of what was measured in 200 rows."""
        repaired, changed = repair_row_artefact(_with_artefact())

        assert changed
        centre = repaired.size // 2
        assert repaired[centre] == pytest.approx(-100.0, abs=1.0)

    def test_the_artefact_is_gone_rather_than_reduced(self):
        """The point is to remove it, not to knock it down to something still misleading."""
        original = _with_artefact()
        repaired, _ = repair_row_artefact(original)

        centre = original.size // 2
        before = original[centre] - np.median(original)
        after = repaired[centre] - np.median(repaired)

        assert before > 4.0
        assert abs(after) < 1.0

    def test_the_whole_measured_range_of_the_artefact_is_removed(self):
        """+4.33 to +5.89 dB is what was seen; all of it has to go."""
        for bump in (4.33, 5.19, 5.89):
            repaired, changed = repair_row_artefact(_with_artefact(bump_db=bump))

            assert changed, f"{bump} dB artefact not recognised"
            centre = repaired.size // 2
            assert repaired[centre] == pytest.approx(-100.0, abs=1.5)

    def test_the_neighbours_outside_the_trio_are_left_alone(self):
        """Only the three bins the artefact occupies are touched."""
        original = _with_artefact()
        repaired, _ = repair_row_artefact(original)
        centre = original.size // 2

        assert np.array_equal(repaired[:centre - 1], original[:centre - 1])
        assert np.array_equal(repaired[centre + 2:], original[centre + 2:])

    def test_a_sloped_row_keeps_its_slope(self):
        """Interpolated, not copied, so a real trend across the row survives the repair."""
        values = np.linspace(-110.0, -90.0, 33).astype(np.float32)
        values[15:18] += 5.0

        repaired, changed = repair_row_artefact(values)

        assert changed
        assert repaired[15] < repaired[16] < repaired[17]
        assert repaired[16] == pytest.approx(-100.0, abs=1.0)


class TestGuard:
    """The half of the fix that stops it destroying data."""

    def test_a_real_carrier_on_the_centre_is_left_alone(self):
        """A survey cannot tell a carrier from the artefact by shape — only by size.

        The artefact never exceeds 5.9 dB, so anything clearly bigger is real and must survive.
        """
        values = _row()
        values[15:18] += 25.0

        repaired, changed = repair_row_artefact(values)

        assert not changed
        assert np.array_equal(repaired, values)

    def test_the_guard_sits_above_the_artefact_it_targets(self):
        """The counter-intuitive part, asserted so it cannot be 'fixed' the wrong way.

        A guard below the artefact's own size would classify the artefact as a real carrier and
        preserve exactly what the function exists to remove.
        """
        assert ROW_ARTEFACT_GUARD_DB > 5.89

    def test_something_exactly_at_the_guard_is_treated_as_real(self):
        """Exact, on a flat row: the boundary is arithmetic, not noise."""
        values = np.full(33, -100.0, dtype=np.float32)
        values[15:18] = -100.0 + ROW_ARTEFACT_GUARD_DB

        _, changed = repair_row_artefact(values)

        assert not changed

    def test_just_under_the_guard_is_treated_as_the_artefact(self):
        values = np.full(33, -100.0, dtype=np.float32)
        values[15:18] = -100.0 + ROW_ARTEFACT_GUARD_DB - 0.5

        _, changed = repair_row_artefact(values)

        assert changed

    def test_a_quiet_row_is_not_flagged_as_corrected(self):
        """Nothing to remove means no copy and no reported change.

        The artefact is always a positive bump. Without a floor, a centre differing from its
        shoulders only by noise would be "corrected" too — replacing three honest samples with
        a smooth estimate and reporting a repair that changed nothing real.
        """
        values = _row()

        repaired, changed = repair_row_artefact(values)

        assert not changed
        assert repaired is values, "an untouched row should not be copied"

    def test_a_bump_below_the_floor_is_left_alone(self):
        values = np.full(33, -100.0, dtype=np.float32)
        values[15:18] = -100.0 + ROW_ARTEFACT_MIN_DB - 0.5

        _, changed = repair_row_artefact(values)

        assert not changed

    def test_a_bump_above_the_floor_is_corrected(self):
        """The measured artefact is 4.33 to 5.89 dB, comfortably inside the band."""
        values = np.full(33, -100.0, dtype=np.float32)
        values[15:18] = -100.0 + ROW_ARTEFACT_MIN_DB + 0.5

        _, changed = repair_row_artefact(values)

        assert changed


class TestRefusals:
    def test_a_row_too_short_to_have_shoulders_is_untouched(self):
        """Without bins either side there is nothing to measure the artefact against."""
        values = np.full(6, -100.0, dtype=np.float32)
        values[3] += 5.0

        repaired, changed = repair_row_artefact(values)

        assert not changed
        assert np.array_equal(repaired, values)

    def test_a_row_that_is_all_nan_is_untouched_rather_than_crashed(self):
        values = np.full(33, np.nan, dtype=np.float32)

        repaired, changed = repair_row_artefact(values)

        assert not changed

    def test_nan_shoulders_do_not_produce_a_nan_repair(self):
        """rtl_power leaves unusable bins as NaN; a repair built from them would spread it."""
        values = _with_artefact()
        values[:15] = np.nan

        repaired, changed = repair_row_artefact(values)

        assert changed is False or np.isfinite(repaired[16])


class TestMergeAppliesIt:
    """The correction has to happen per row during the merge, not on the stitched result."""

    def _sub_band(self, low_hz: float, *, seed: int) -> PowerRow:
        values = _with_artefact(seed=seed)
        high_hz = low_hz + (values.size - 1) * 81_250.0
        return PowerRow(low_hz, high_hz, 81_250.0, 100, values)

    def test_every_sub_bands_centre_is_corrected(self):
        first = self._sub_band(902e6, seed=1)
        second = self._sub_band(904.6e6, seed=2)

        merged = merge_rows([first, second])

        # The two artefact trios sit at each row's centre, which after stitching are separate
        # places in the merged row.
        for row in (first, second):
            centre_index = int(round((row.low_hz + (row.power_db.size // 2) * 81_250.0
                                      - merged.low_hz) / 81_250.0))
            local = merged.power_db[centre_index]
            assert abs(local - np.nanmedian(merged.power_db)) < 3.0, "artefact still present"

    def test_a_real_carrier_survives_the_merge(self):
        row = self._sub_band(902e6, seed=3)
        boosted = row.power_db.copy()
        boosted[row.power_db.size // 2] += 30.0
        loud = PowerRow(row.low_hz, row.high_hz, row.step_hz, row.samples, boosted)

        merged = merge_rows([loud])
        centre_index = loud.power_db.size // 2

        assert merged.power_db[centre_index] > np.nanmedian(merged.power_db) + 20
