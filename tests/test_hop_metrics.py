"""Tests for analytics.hop_metrics."""
import pytest
from meshchat.analytics.hop_metrics import (
    compute_hops_used,
    hop_efficiency_bucket,
    is_direct_rf,
    percent_hops_used,
)


# ── compute_hops_used ─────────────────────────────────────────────────────────

class TestComputeHopsUsed:
    def test_zero_hops(self):
        """hopStart == hopLimit → direct RF packet."""
        assert compute_hops_used(3, 3) == 0

    def test_one_hop(self):
        assert compute_hops_used(3, 2) == 1

    def test_two_hops(self):
        assert compute_hops_used(3, 1) == 2

    def test_max_hops(self):
        assert compute_hops_used(7, 0) == 7

    def test_none_hop_start_returns_none(self):
        assert compute_hops_used(None, 3) is None

    def test_none_hop_limit_returns_none(self):
        assert compute_hops_used(3, None) is None

    def test_both_none_returns_none(self):
        assert compute_hops_used(None, None) is None

    def test_hop_start_zero_returns_none(self):
        """hopStart == 0 means old firmware; direction is unknown."""
        assert compute_hops_used(0, 0) is None

    def test_hop_limit_greater_than_hop_start_invalid(self):
        """hopLimit > hopStart is an impossible field combination."""
        assert compute_hops_used(2, 5) is None

    def test_hop_start_one_hop_limit_one(self):
        """hop_start=1, hop_limit=1 → 0 hops used."""
        assert compute_hops_used(1, 1) == 0


# ── is_direct_rf ──────────────────────────────────────────────────────────────

class TestIsDirectRf:
    def test_confirmed_direct(self):
        assert is_direct_rf(hops_used=0, hop_start=3, via_mqtt=False) is True

    def test_one_hop_not_direct(self):
        assert is_direct_rf(hops_used=1, hop_start=3, via_mqtt=False) is False

    def test_zero_hops_but_via_mqtt_not_direct(self):
        assert is_direct_rf(hops_used=0, hop_start=3, via_mqtt=True) is False

    def test_zero_hops_hop_start_zero_not_direct(self):
        """hop_start == 0 is untrusted; cannot confirm direct."""
        assert is_direct_rf(hops_used=0, hop_start=0, via_mqtt=False) is False

    def test_none_hops_used_not_direct(self):
        assert is_direct_rf(hops_used=None, hop_start=3, via_mqtt=False) is False

    def test_none_hop_start_not_direct(self):
        assert is_direct_rf(hops_used=0, hop_start=None, via_mqtt=False) is False

    def test_via_mqtt_none_treated_as_not_mqtt(self):
        """via_mqtt=None means we don't know; should not block the direct flag."""
        assert is_direct_rf(hops_used=0, hop_start=3, via_mqtt=None) is True


# ── percent_hops_used ────────────────────────────────────────────────────────

class TestPercentHopsUsed:
    def test_direct(self):
        assert percent_hops_used(0, 3) == pytest.approx(0.0)

    def test_one_of_three(self):
        assert percent_hops_used(1, 3) == pytest.approx(1 / 3)

    def test_full_hops(self):
        assert percent_hops_used(3, 3) == pytest.approx(1.0)

    def test_none_hops_used(self):
        assert percent_hops_used(None, 3) is None

    def test_none_hop_start(self):
        assert percent_hops_used(0, None) is None

    def test_hop_start_zero_returns_none(self):
        assert percent_hops_used(0, 0) is None


# ── hop_efficiency_bucket ─────────────────────────────────────────────────────

class TestHopEfficiencyBucket:
    def test_none_is_unknown(self):
        assert hop_efficiency_bucket(None) == "Unknown"

    def test_zero_is_zero(self):
        assert hop_efficiency_bucket(0.0) == "0%"

    def test_boundary_1_percent(self):
        assert hop_efficiency_bucket(0.01) == "1–24%"

    def test_boundary_25_percent(self):
        assert hop_efficiency_bucket(0.25) == "1–24%"

    def test_boundary_26_percent(self):
        assert hop_efficiency_bucket(0.26) == "25–49%"

    def test_boundary_50_percent(self):
        assert hop_efficiency_bucket(0.50) == "25–49%"

    def test_boundary_51_percent(self):
        assert hop_efficiency_bucket(0.51) == "50–74%"

    def test_boundary_75_percent(self):
        assert hop_efficiency_bucket(0.75) == "50–74%"

    def test_boundary_76_percent(self):
        assert hop_efficiency_bucket(0.76) == "75–99%"

    def test_just_under_100_percent(self):
        assert hop_efficiency_bucket(0.999) == "75–99%"

    def test_exactly_100_percent(self):
        assert hop_efficiency_bucket(1.0) == "100%"
