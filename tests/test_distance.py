"""Tests for analytics.distance (Haversine formula)."""
import math
import pytest
from meshchat.analytics.distance import (
    bearing_deg,
    format_distance,
    haversine_km,
    haversine_mi,
)


# ── haversine_km ──────────────────────────────────────────────────────────────

class TestHaversineKm:
    def test_same_point_is_zero(self):
        assert haversine_km(51.5, -0.12, 51.5, -0.12) == pytest.approx(0.0, abs=1e-9)

    def test_known_distance_london_to_paris(self):
        """London (51.5074°N, 0.1278°W) to Paris (48.8566°N, 2.3522°E)."""
        km = haversine_km(51.5074, -0.1278, 48.8566, 2.3522)
        # Well-known approximate distance is ~340 km
        assert 335.0 < km < 345.0

    def test_equator_longitude_wrap(self):
        """Cross the antimeridian: (0, 179.9) to (0, -179.9)."""
        km = haversine_km(0.0, 179.9, 0.0, -179.9)
        # Should be ~22.2 km (0.2 degrees of longitude at equator)
        expected = 2 * math.pi * 6371.0 * 0.2 / 360
        assert km == pytest.approx(expected, rel=0.01)

    def test_north_to_south_pole(self):
        """Antipodal: (90, 0) to (-90, 0) → half Earth circumference."""
        km = haversine_km(90.0, 0.0, -90.0, 0.0)
        assert km == pytest.approx(math.pi * 6371.0, rel=0.001)

    def test_small_distance_accuracy(self):
        """Two points 100m apart (within floating-point accuracy)."""
        # ~0.0009 degrees latitude ≈ 100 m
        km = haversine_km(37.0, -122.0, 37.0009, -122.0)
        assert 0.09 < km < 0.11

    def test_symmetric(self):
        """haversine_km(A, B) == haversine_km(B, A)."""
        km_ab = haversine_km(40.0, -74.0, 51.5, -0.12)
        km_ba = haversine_km(51.5, -0.12, 40.0, -74.0)
        assert km_ab == pytest.approx(km_ba)


# ── haversine_mi ──────────────────────────────────────────────────────────────

class TestHaversineMi:
    def test_km_to_miles_conversion(self):
        km = haversine_km(51.5074, -0.1278, 48.8566, 2.3522)
        mi = haversine_mi(51.5074, -0.1278, 48.8566, 2.3522)
        assert mi == pytest.approx(km * 0.621371, rel=0.0001)

    def test_same_point_is_zero(self):
        assert haversine_mi(0.0, 0.0, 0.0, 0.0) == pytest.approx(0.0, abs=1e-9)


# ── bearing_deg ───────────────────────────────────────────────────────────────

class TestBearingDeg:
    def test_north(self):
        """Directly north → bearing 0°."""
        b = bearing_deg(0.0, 0.0, 1.0, 0.0)
        assert b == pytest.approx(0.0, abs=0.01)

    def test_east(self):
        """Directly east on equator → bearing 90°."""
        b = bearing_deg(0.0, 0.0, 0.0, 1.0)
        assert b == pytest.approx(90.0, abs=0.01)

    def test_south(self):
        """Directly south → bearing 180°."""
        b = bearing_deg(1.0, 0.0, 0.0, 0.0)
        assert b == pytest.approx(180.0, abs=0.01)

    def test_west(self):
        """Directly west on equator → bearing 270°."""
        b = bearing_deg(0.0, 1.0, 0.0, 0.0)
        assert b == pytest.approx(270.0, abs=0.01)

    def test_range_is_0_to_360(self):
        """Bearing must always be in [0, 360)."""
        for lat1, lon1, lat2, lon2 in [
            (10, 20, 30, 40),
            (50, -10, -30, 120),
            (-80, 170, 80, -170),
        ]:
            b = bearing_deg(lat1, lon1, lat2, lon2)
            assert 0.0 <= b < 360.0


# ── format_distance ───────────────────────────────────────────────────────────

class TestFormatDistance:
    def test_meters_metric(self):
        assert format_distance(0.5) == "500 m"

    def test_km_metric(self):
        assert format_distance(1.5) == "1.50 km"

    def test_feet_imperial(self):
        # 0.03 km ≈ 98 ft; < 0.1 mi → feet
        result = format_distance(0.03, use_metric=False)
        assert result.endswith("ft")

    def test_miles_imperial(self):
        # 5 km ≈ 3.11 mi
        result = format_distance(5.0, use_metric=False)
        assert result.endswith("mi")
        assert "3.1" in result

    def test_exact_1km_metric(self):
        assert format_distance(1.0) == "1.00 km"

    def test_below_1km_metric(self):
        assert format_distance(0.1) == "100 m"
