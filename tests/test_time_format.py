"""Tests for shared relative-time formatting.

Four copies of this logic had drifted: only the Nodes-table one rolled over to
days, so a node last heard 134 days ago showed "134d" there and "3227h" in the
Last Heard ranking for the same value. These tests pin the behaviour that all
call sites now share.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from meshchat.utils.time_format import format_elapsed, relative_age


def _ago(**kwargs) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kwargs)


class TestRelativeAge:
    def test_none_uses_default_placeholder(self):
        assert relative_age(None) == "—"

    def test_none_respects_custom_placeholder(self):
        assert relative_age(None, placeholder="?") == "?"

    def test_seconds(self):
        assert relative_age(_ago(seconds=45)) == "45s"

    def test_just_under_a_minute_stays_seconds(self):
        assert relative_age(_ago(seconds=59)).endswith("s")

    def test_minutes(self):
        assert relative_age(_ago(minutes=5)) == "5m"

    def test_minutes_floor_not_round(self):
        # 90 minutes is 1h, not 2h — floor division, not rounding
        assert relative_age(_ago(minutes=90)) == "1h"

    def test_hours(self):
        assert relative_age(_ago(hours=3)) == "3h"

    def test_rolls_over_to_days(self):
        # The regression: this used to render as "3227h"
        assert relative_age(_ago(days=134, hours=11)) == "134d"

    def test_boundary_just_under_a_day_is_hours(self):
        assert relative_age(_ago(hours=23, minutes=59)) == "23h"

    def test_boundary_exactly_a_day_is_days(self):
        assert relative_age(_ago(days=1, seconds=1)) == "1d"

    def test_suffix_is_appended_to_real_values(self):
        assert relative_age(_ago(hours=2), suffix=" ago") == "2h ago"

    def test_suffix_is_not_appended_to_placeholder(self):
        # "— ago" would be nonsense
        assert relative_age(None, suffix=" ago") == "—"

    def test_future_timestamp_clamps_instead_of_going_negative(self):
        # Clock skew between this machine and the mesh must not render "-2h"
        assert relative_age(datetime.now(timezone.utc) + timedelta(hours=2)) == "0s"

    @pytest.mark.parametrize("delta,expected_unit", [
        (timedelta(seconds=1), "s"),
        (timedelta(minutes=1), "m"),
        (timedelta(hours=1), "h"),
        (timedelta(days=1, seconds=1), "d"),
    ])
    def test_unit_selection(self, delta, expected_unit):
        assert relative_age(datetime.now(timezone.utc) - delta).endswith(expected_unit)


class TestFormatElapsed:
    def test_seconds(self):
        assert format_elapsed(45) == "45s"

    def test_minutes(self):
        assert format_elapsed(12 * 60) == "12m"

    def test_hours_and_minutes(self):
        assert format_elapsed(3 * 3600 + 20 * 60) == "3h 20m"

    def test_days_and_hours(self):
        assert format_elapsed(2 * 86400 + 5 * 3600) == "2d 5h"

    def test_zero(self):
        assert format_elapsed(0) == "0s"

    def test_exact_hour_shows_zero_minutes(self):
        assert format_elapsed(3600) == "1h 0m"
