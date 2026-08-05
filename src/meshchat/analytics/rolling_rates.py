"""MeshChat – analytics: rolling time-window packet rates."""
from __future__ import annotations

import time
from collections import deque
from threading import Lock


class RollingRate:
    """
    Counts events in a sliding time window.

    Thread-safe. Uses monotonic time internally.
    """

    def __init__(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = Lock()

    def record(self, t: float | None = None) -> None:
        """Record an event at time t (default: now)."""
        ts = t if t is not None else time.monotonic()
        with self._lock:
            self._timestamps.append(ts)
            self._prune(ts)

    def count(self, now: float | None = None) -> int:
        """Return the number of events in the last window_seconds."""
        ts = now if now is not None else time.monotonic()
        with self._lock:
            self._prune(ts)
            return len(self._timestamps)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()


class DualRateTracker:
    """Tracks both per-minute and per-hour rolling rates together."""

    def __init__(self) -> None:
        self._per_min = RollingRate(60.0)
        self._per_hour = RollingRate(3600.0)

    def record(self, t: float | None = None) -> None:
        ts = t if t is not None else time.monotonic()
        self._per_min.record(ts)
        self._per_hour.record(ts)

    @property
    def per_minute(self) -> int:
        return self._per_min.count()

    @property
    def per_hour(self) -> int:
        return self._per_hour.count()

    def reset(self) -> None:
        self._per_min.reset()
        self._per_hour.reset()
