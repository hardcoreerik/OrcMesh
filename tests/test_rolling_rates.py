"""Tests for analytics.rolling_rates."""
import threading
import pytest
from meshchat.analytics.rolling_rates import DualRateTracker, RollingRate


# ── RollingRate ───────────────────────────────────────────────────────────────

class TestRollingRate:
    def test_empty_count_is_zero(self):
        r = RollingRate(60.0)
        assert r.count(now=0.0) == 0

    def test_record_and_count_within_window(self):
        r = RollingRate(60.0)
        r.record(t=100.0)
        r.record(t=110.0)
        r.record(t=120.0)
        assert r.count(now=150.0) == 3

    def test_events_outside_window_are_pruned(self):
        r = RollingRate(60.0)
        r.record(t=100.0)   # 61 seconds before now=161 → outside
        r.record(t=130.0)   # inside
        r.record(t=150.0)   # inside
        assert r.count(now=161.0) == 2

    def test_event_at_cutoff_boundary_excluded(self):
        """Event exactly at (now - window) is excluded by the <= cutoff check."""
        r = RollingRate(60.0)
        r.record(t=100.0)
        # now=160 → cutoff=100; t=100 is <= cutoff → excluded
        assert r.count(now=160.0) == 0

    def test_event_just_after_cutoff_included(self):
        r = RollingRate(60.0)
        r.record(t=100.001)
        assert r.count(now=160.0) == 1

    def test_reset_clears_all(self):
        r = RollingRate(60.0)
        r.record(t=100.0)
        r.record(t=110.0)
        r.reset()
        assert r.count(now=150.0) == 0

    def test_large_burst_sliding_window(self):
        """Record 500 events from t=0..499; at t=499 only last 60 are in window."""
        r = RollingRate(60.0)
        for i in range(500):
            r.record(t=float(i))
        # Events at t=440..499 are inside the 60-second window at now=499
        # t=440 → cutoff=499-60=439; 440 > 439 so included. 60 events total.
        assert r.count(now=499.0) == 60

    def test_thread_safe_concurrent_records(self):
        """Concurrent records from multiple threads must not corrupt state."""
        r = RollingRate(3600.0)
        errors = []

        def worker(base: float) -> None:
            try:
                for i in range(250):
                    r.record(t=base + i * 0.001)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(float(i * 10_000),)) for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors, f"Thread errors: {errors}"


# ── DualRateTracker ───────────────────────────────────────────────────────────

class TestDualRateTracker:
    def test_per_minute_counts_only_last_60s(self):
        now = 10_000.0
        tracker = DualRateTracker()
        tracker._per_min.record(t=now - 120)  # outside 60s window
        tracker._per_min.record(t=now - 30)   # inside
        assert tracker._per_min.count(now=now) == 1

    def test_per_hour_counts_up_to_3600s(self):
        now = 10_000.0
        tracker = DualRateTracker()
        for delta in [4000, 3000, 2000, 1000, 500, 100]:
            tracker._per_hour.record(t=now - delta)
        # delta=4000 → t=6000, now=10000, cutoff=6400 → 6000 < 6400 → excluded
        assert tracker._per_hour.count(now=now) == 5

    def test_record_increments_both_windows(self):
        tracker = DualRateTracker()
        t = 100_000.0
        tracker._per_min.record(t=t)
        tracker._per_hour.record(t=t)
        assert tracker._per_min.count(now=t + 1) == 1
        assert tracker._per_hour.count(now=t + 1) == 1

    def test_reset_clears_both(self):
        tracker = DualRateTracker()
        t = 50_000.0
        tracker._per_min.record(t=t)
        tracker._per_hour.record(t=t)
        tracker.reset()
        assert tracker._per_min.count(now=t + 1) == 0
        assert tracker._per_hour.count(now=t + 1) == 0

    def test_concurrent_records_no_crash(self):
        """Smoke-test: concurrent record() calls do not deadlock or crash."""
        tracker = DualRateTracker()
        errors = []

        def worker(base: float) -> None:
            try:
                for i in range(200):
                    tracker.record(t=base + i * 0.001)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(float(i * 1000),)) for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors
