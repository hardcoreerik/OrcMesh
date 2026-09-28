"""Tests for slot occupancy ranking.

Synthetic bands with known signals, so every number can be checked by hand, plus
one test against the real US band plan to catch a change in its marker output.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshchat.analytics.lora_bands import MESHTASTIC_REGIONS, ChannelMarker, meshtastic_markers
from meshchat.analytics.slot_occupancy import (
    IDLE_MARGIN_DB,
    STATUS_BUSY,
    STATUS_IDLE,
    STATUS_UNKNOWN,
    rank_slots,
)

#: Matches what rtl_power picks over 902-928 MHz at 2.4-2.56 MS/s.
BIN_HZ = 81_250.0
_LOW_HZ = 902_000_000.0
_FLOOR_DB = -95.0


def _band(bins: int = 321):
    return _LOW_HZ + np.arange(bins) * BIN_HZ


def _power(bins: int = 321) -> np.ndarray:
    return np.full(bins, _FLOOR_DB, dtype=np.float64)


def _slot(label: str, center_mhz: float, bandwidth_khz: float = 125.0) -> ChannelMarker:
    return ChannelMarker(label=label, center_mhz=center_mhz, bandwidth_khz=bandwidth_khz)


def _inject(power: np.ndarray, frequencies: np.ndarray, center_hz: float, db: float) -> None:
    """Raise the single bin nearest `center_hz` to `db`."""
    index = int(np.argmin(np.abs(frequencies - center_hz)))
    power[index] = max(power[index], db)


class TestRanking:
    def test_a_signal_puts_its_slot_first(self):
        frequencies = _band()
        power = _power()
        quiet = _slot("quiet", 905.0)
        loud = _slot("loud", 915.0)
        _inject(power, frequencies, 915.0e6, -40.0)

        report = rank_slots(frequencies, power, [quiet, loud])

        assert [slot.label for slot in report.slots] == ["loud", "quiet"]

    def test_the_winner_reports_how_far_above_the_floor_it_sits(self):
        frequencies = _band()
        power = _power()
        _inject(power, frequencies, 915.0e6, -45.0)

        report = rank_slots(frequencies, power, [_slot("hot", 915.0)])

        slot = report.slots[0]
        assert slot.peak_db == pytest.approx(-45.0)
        assert slot.noise_floor_db == pytest.approx(_FLOOR_DB)
        assert slot.peak_over_floor_db == pytest.approx(50.0)
        assert slot.status == STATUS_BUSY

    def test_a_slot_at_the_floor_reads_as_idle_not_busy(self):
        frequencies = _band()
        power = _power()

        report = rank_slots(frequencies, power, [_slot("empty", 915.0)])

        assert report.slots[0].status == STATUS_IDLE
        assert not report.slots[0].looks_busy

    def test_a_slot_just_barely_above_the_floor_is_still_idle(self):
        """The margin exists so noise wobble is not mistaken for traffic."""
        frequencies = _band()
        power = _power()
        _inject(power, frequencies, 915.0e6, _FLOOR_DB + IDLE_MARGIN_DB - 1.0)

        report = rank_slots(frequencies, power, [_slot("wobble", 915.0)])

        assert report.slots[0].status == STATUS_IDLE

    def test_it_ranks_by_peak_not_by_mean(self):
        """A narrow burst must beat a broad slight lift — LoRa is bursty."""
        frequencies = _band()
        power = _power() + 3.0  # the whole band slightly raised
        _inject(power, frequencies, 915.0e6, -30.0)
        burst = _slot("burst", 915.0, bandwidth_khz=125.0)
        smear = _slot("smear", 920.0, bandwidth_khz=2000.0)

        report = rank_slots(frequencies, power, [smear, burst])

        assert report.slots[0].label == "burst", "the peak finds what the mean dilutes"


class TestUnmeasuredSlots:
    def test_a_slot_with_no_measured_bins_is_unknown_not_idle(self):
        """Claiming a slot is quiet when it was never measured would be a lie."""
        frequencies = _band()
        power = _power()
        power[:] = np.nan

        report = rank_slots(frequencies, power, [_slot("unseen", 915.0)])

        slot = report.slots[0]
        assert slot.bins == 0
        assert slot.status == STATUS_UNKNOWN
        assert not slot.has_data
        assert np.isnan(slot.peak_over_floor_db)

    def test_an_unknown_slot_sorts_after_a_measured_idle_one(self):
        """Unknown must not be ranked as if its value were zero."""
        frequencies = _band()
        power = _power()
        power[:64] = np.nan  # wipe the low end, where the first slot sits
        unknown = _slot("unknown", 902.2)
        idle = _slot("idle", 915.0)

        report = rank_slots(frequencies, power, [unknown, idle])

        assert [slot.label for slot in report.slots] == ["idle", "unknown"]
        assert report.unknown_slots == 1
        assert report.measured_slots == 1

    def test_hottest_skips_unknown_slots(self):
        frequencies = _band()
        power = _power()
        power[:] = np.nan

        report = rank_slots(frequencies, power, [_slot("unseen", 915.0)])

        assert report.hottest is None

    def test_a_nan_bin_inside_a_slot_does_not_poison_its_reading(self):
        """One lost bin must not make a whole slot unmeasurable."""
        frequencies = _band()
        power = _power()
        _inject(power, frequencies, 915.0e6, -40.0)
        index = int(np.argmin(np.abs(frequencies - 915.0e6)))
        power[index - 1] = np.nan

        report = rank_slots(frequencies, power, [_slot("hot", 915.0)])

        assert report.slots[0].has_data
        assert report.slots[0].peak_db == pytest.approx(-40.0)


class TestGeometry:
    def test_a_slot_beyond_the_scan_is_not_claimed_as_measured(self):
        frequencies = _band()  # 902.0 - 928.0 MHz
        power = _power()

        report = rank_slots(frequencies, power, [_slot("beyond", 930.0)])

        assert report.slots[0].in_scan is False
        assert report.slots[0].status == STATUS_UNKNOWN

    def test_a_slot_at_the_edge_of_the_scan_is_in_scan(self):
        frequencies = _band()
        power = _power()

        report = rank_slots(frequencies, power, [_slot("low-edge", 902.0)])

        assert report.slots[0].in_scan is True

    def test_a_slot_catches_every_bin_it_covers(self):
        """A 250 kHz slot against 81.25 kHz bins spans three or four of them."""
        frequencies = _band()
        power = _power()
        slot = _slot("wide", 915.0, bandwidth_khz=250.0)

        report = rank_slots(frequencies, power, [slot])

        assert report.slots[0].bins >= 3

    def test_a_narrow_slot_still_finds_its_bin(self):
        """The resolution limit: a 125 kHz slot is one or two bins wide here."""
        frequencies = _band()
        power = _power()

        report = rank_slots(frequencies, power, [_slot("narrow", 915.0, 125.0)])

        assert report.slots[0].bins >= 1

    def test_mean_and_peak_come_from_the_slots_own_bins(self):
        frequencies = np.array([0.0, 1000.0, 2000.0, 3000.0])
        power = np.array([-90.0, -40.0, -50.0, -90.0])
        slot = _slot("middle", 0.0015, bandwidth_khz=1.0)  # covers 1000-2000 Hz

        report = rank_slots(frequencies, power, [slot])

        assert report.slots[0].bins == 2
        assert report.slots[0].peak_db == pytest.approx(-40.0)
        assert report.slots[0].mean_db == pytest.approx(-45.0)

    def test_the_scanned_range_is_reported(self):
        frequencies = _band(11)

        report = rank_slots(frequencies, _power(11), [])

        assert report.scanned_low_hz == pytest.approx(_LOW_HZ)
        assert report.scanned_high_hz == pytest.approx(_LOW_HZ + 10 * BIN_HZ)


class TestNoiseFloor:
    def test_a_busy_band_does_not_raise_its_own_floor(self):
        """The reason the floor is a low percentile and not the median."""
        frequencies = _band(101)
        power = np.full(101, _FLOOR_DB, dtype=np.float64)
        # Most of the band carrying strong signals.
        power[:70] = -30.0

        report = rank_slots(frequencies, power, [])

        assert report.noise_floor_db == pytest.approx(_FLOOR_DB), "floor stayed put"

    def test_an_all_nan_band_has_no_floor_and_measures_nothing(self):
        frequencies = _band()
        power = np.full(321, np.nan)

        report = rank_slots(frequencies, power, [_slot("slot", 915.0)])

        assert np.isnan(report.noise_floor_db)
        assert report.slots[0].status == STATUS_UNKNOWN


class TestEdges:
    def test_no_markers_gives_an_empty_report(self):
        report = rank_slots(_band(), _power(), [])

        assert report.slots == ()
        assert report.hottest is None
        assert report.busy_slots == 0

    def test_empty_arrays_do_not_crash(self):
        report = rank_slots(np.array([]), np.array([]), [_slot("slot", 915.0)])

        assert report.slots[0].status == STATUS_UNKNOWN

    def test_mismatched_lengths_are_rejected(self):
        with pytest.raises(ValueError):
            rank_slots(np.array([1.0, 2.0]), np.array([1.0]), [])

    def test_ties_are_broken_deterministically(self):
        """Equal signals must not reorder between sweeps."""
        frequencies = _band()
        power = _power()
        _inject(power, frequencies, 910.0e6, -40.0)
        _inject(power, frequencies, 920.0e6, -40.0)

        first = rank_slots(frequencies, power, [_slot("bbb", 910.0), _slot("aaa", 920.0)])
        second = rank_slots(frequencies, power, [_slot("aaa", 920.0), _slot("bbb", 910.0)])

        assert [slot.label for slot in first.slots] == ["aaa", "bbb"]
        assert [slot.label for slot in first.slots] == [slot.label for slot in second.slots]


class TestRealBandPlan:
    def test_real_us_markers_land_inside_the_us_band(self):
        """Ties this to the band plan the Spectrum page draws from."""
        band = MESHTASTIC_REGIONS["US"]
        markers = meshtastic_markers("US", "LONG_FAST", 0, include_neighbours=4)

        assert markers, "the US plan must produce markers"

        for marker in markers:
            assert band.start_mhz <= marker.center_mhz <= band.end_mhz

    def test_real_markers_can_be_ranked_over_a_scanned_band(self):
        frequencies = _band()  # 902-928 MHz, matching the US band
        power = _power()
        markers = meshtastic_markers("US", "LONG_FAST", 0, include_neighbours=4)
        _inject(power, frequencies, markers[0].center_mhz * 1e6, -35.0)

        report = rank_slots(frequencies, power, markers)

        assert len(report.slots) == len(markers)
        assert report.hottest is not None
        assert report.hottest.peak_over_floor_db > 40.0
        assert report.measured_slots == len(markers), "all slots are inside the scan"
