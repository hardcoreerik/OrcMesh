"""Tests for region size estimation.

The point of these is calibration, not coverage: every assertion below either
reproduces a *measured* build or pins the honesty of the range, because a wizard
that quotes a confident wrong size is worse than one that quotes a range.

The strongest test here is the one the model was not fitted to: it predicts a
United States z0-9 pack from Oregon's density, and that build exists on this
machine (261 MB).
"""
from __future__ import annotations

import pytest

from meshchat.services.provisioning.regions import (
    DENSITY_BYTES_PER_KM2_Z13,
    DENSITY_BYTES_PER_KM2_Z9,
    MEASURED_PIN_CUTS,
    TIER_MAX_ZOOM,
    PackSizeEstimate,
    bbox_area_km2,
    estimate_area_pack_bytes,
    estimate_radius_pack_bytes,
    estimate_region_bytes,
    zoom_scale,
)

# Measured on this machine, so the tests are anchored to reality rather than to
# the constants they are checking.
OREGON_AREA_KM2 = 254_800
OREGON_Z13_BYTES = 84_615_534
OREGON_Z9_BYTES = 8_360_000
US_AREA_KM2 = 9_161_000
US_Z9_BYTES = 261_360_000


class TestMeasuredPinCuts:
    def test_the_measured_points_are_reproduced_exactly(self):
        """At the nodes the estimate must equal the measurement, not approach it."""
        for radius_km, size_bytes in MEASURED_PIN_CUTS:
            estimate = estimate_radius_pack_bytes(radius_km, max_zoom=13)
            assert estimate.expected == size_bytes, (
                f"the {radius_km:.0f} km node should reproduce its measurement"
            )

    def test_the_band_always_brackets_the_measurement(self):
        for radius_km, size_bytes in MEASURED_PIN_CUTS:
            estimate = estimate_radius_pack_bytes(radius_km)
            assert estimate.low <= size_bytes <= estimate.high

    def test_a_bigger_radius_estimates_bigger(self):
        sizes = [estimate_radius_pack_bytes(r).expected for r in (10, 25, 50, 100, 200)]
        assert sizes == sorted(sizes), "estimates must be monotonic in radius"

    def test_beyond_the_measured_range_it_extrapolates_upward(self):
        """No measured node past 100 km, so growth must continue, not plateau."""
        hundred = estimate_radius_pack_bytes(100).expected
        two_hundred = estimate_radius_pack_bytes(200).expected
        assert two_hundred > hundred

    def test_a_degenerate_radius_does_not_explode(self):
        estimate = estimate_radius_pack_bytes(0.0)
        assert 0 < estimate.expected < estimate_radius_pack_bytes(25).expected

    def test_the_basis_names_the_measurement_it_came_from(self):
        estimate = estimate_radius_pack_bytes(25)
        assert "25/50/100 km" in estimate.basis
        assert "84 MB" in estimate.basis


class TestDensityModel:
    def test_it_reproduces_the_oregon_z13_measurement(self):
        estimate = estimate_area_pack_bytes(OREGON_AREA_KM2, max_zoom=13)

        assert estimate.expected == pytest.approx(OREGON_Z13_BYTES, rel=0.01)

    def test_it_reproduces_the_oregon_z9_measurement(self):
        """The z9 point was measured separately, so this cross-checks the curve."""
        estimate = estimate_area_pack_bytes(OREGON_AREA_KM2, max_zoom=9)

        assert estimate.expected == pytest.approx(OREGON_Z9_BYTES, rel=0.10)

    def test_it_predicts_the_united_states_build_it_was_not_fitted_to(self):
        """Oregon density scaled to US land area vs the real 261 MB z0-9 pack.

        This is the honest calibration story in one test: the prediction was
        153 MB by raw density, the build came out 1.7x heavier, and the band is
        wide precisely because dense states beat a rural state's average.
        """
        estimate = estimate_area_pack_bytes(US_AREA_KM2, max_zoom=9)

        assert estimate.low <= US_Z9_BYTES <= estimate.high, (
            f"the real US build ({US_Z9_BYTES / 1e6:.0f} MB) must fall in "
            f"{estimate.format_range()}"
        )

    def test_the_zoom_scale_matches_the_measured_density_ratio(self):
        assert zoom_scale(13) == 1.0
        assert zoom_scale(9) == pytest.approx(
            DENSITY_BYTES_PER_KM2_Z9 / DENSITY_BYTES_PER_KM2_Z13, rel=0.02
        )

    def test_a_deeper_ceiling_estimates_more(self):
        shallow = estimate_area_pack_bytes(OREGON_AREA_KM2, max_zoom=9).expected
        deep = estimate_area_pack_bytes(OREGON_AREA_KM2, max_zoom=13).expected
        assert deep > shallow
        assert DENSITY_BYTES_PER_KM2_Z13 > DENSITY_BYTES_PER_KM2_Z9

    def test_the_basis_states_the_measurement_and_the_area(self):
        estimate = estimate_area_pack_bytes(1_000, max_zoom=12)
        assert "332 B/km" in estimate.basis
        assert "1,000 km" in estimate.basis
        assert "z<=12" in estimate.basis


class TestBoundingBoxArea:
    def test_a_degree_square_at_the_equator(self):
        assert bbox_area_km2(0.0, 0.0, 1.0, 1.0) == pytest.approx(12_392, rel=0.01)

    def test_a_degree_square_shrinks_with_latitude(self):
        equator = bbox_area_km2(0.0, 0.0, 1.0, 1.0)
        north = bbox_area_km2(0.0, 44.0, 1.0, 45.0)
        assert north < equator
        assert north == pytest.approx(equator * 0.707, rel=0.03)

    def test_a_rectangle_is_not_square_metres(self):
        assert bbox_area_km2(-124.0, 43.0, -122.0, 45.0) == pytest.approx(
            4 * bbox_area_km2(0.0, 43.0, 1.0, 44.0), rel=0.01
        )

    def test_ordering_of_the_edges_does_not_matter(self):
        forward = bbox_area_km2(-124.0, 43.0, -122.0, 45.0)
        reversed_ = bbox_area_km2(-122.0, 45.0, -124.0, 43.0)
        assert forward == pytest.approx(reversed_)


class TestEstimateRegionBytes:
    def test_a_radius_gives_a_radius_estimate(self):
        via_facade = estimate_region_bytes(radius_km=50, max_zoom=13)
        direct = estimate_radius_pack_bytes(50, max_zoom=13)
        assert via_facade == direct

    def test_a_bbox_gives_a_density_estimate(self):
        bbox = (-124.0, 43.0, -122.0, 45.0)
        via_facade = estimate_region_bytes(bbox=bbox, max_zoom=13)
        direct = estimate_area_pack_bytes(bbox_area_km2(*bbox), max_zoom=13)
        assert via_facade == direct

    def test_neither_shape_is_an_error(self):
        with pytest.raises(ValueError, match="exactly one"):
            estimate_region_bytes()

    def test_both_shapes_is_an_error(self):
        """Silently preferring one would quote a size for the wrong region."""
        with pytest.raises(ValueError, match="exactly one"):
            estimate_region_bytes(radius_km=25, bbox=(-124.0, 43.0, -122.0, 45.0))


class TestPresentation:
    def test_the_range_reads_as_megabytes(self):
        assert estimate_radius_pack_bytes(25).format_range() == "2–8 MB"

    def test_the_expected_value_reads_as_megabytes(self):
        assert estimate_radius_pack_bytes(100).format_expected() == "20 MB"

    def test_an_estimate_is_immutable(self):
        estimate = estimate_radius_pack_bytes(25)
        with pytest.raises(Exception):
            estimate.expected = 0  # type: ignore[misc]

    def test_low_never_exceeds_high(self):
        for radius in (1, 25, 100, 500):
            estimate = estimate_radius_pack_bytes(radius)
            assert isinstance(estimate, PackSizeEstimate)
            assert estimate.low <= estimate.expected <= estimate.high


class TestTiers:
    def test_the_two_tiers_are_region_then_local(self):
        assert set(TIER_MAX_ZOOM) == {"region", "local"}
        assert TIER_MAX_ZOOM["region"] < TIER_MAX_ZOOM["local"]
