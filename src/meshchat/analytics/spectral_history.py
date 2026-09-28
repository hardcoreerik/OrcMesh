"""Long-term statistics over a stream of spectra — the engine behind the analysis modes.

A live waterfall shows the *last* moment, which is the wrong thing to look at for most SIGINT
questions. "Is anything here ever?" is answered from minutes of history, not from the row on
screen, and the signals worth finding are usually the ones that are *not* there at the instant
you look. Everything in this module exists to answer that class of question from the same row
stream the waterfall consumes.

Five things are computed, one per analysis mode:

* **peak hold** — the maximum each bin has ever reached. An intermittent emitter that appears
  for a fraction of a second still leaves a mark, which is the single most useful thing to
  have when hunting something that will not sit still.
* **occupancy** — what fraction of the time each bin was above the floor. Different from
  strength: a weak carrier that is always present and a strong one that appears once look
  identical on peak hold, and completely different here.
* **percentiles** — the distribution per bin, so a value can be read as "typical" or "unusual"
  rather than as a single sample.
* **slot grid** — power folded onto LoRa channel slots over time, which turns a spectrum into a
  question about the mesh's own layout.
* **bursts** — discrete events with a start, an end, a width and a peak, which is what a log of
  what happened looks like as opposed to a picture of what is happening.

Pure: no Qt, no devices, no I/O. The whole module is testable with arrays, which is why the
statistics are separable from the widgets that draw them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

log = logging.getLogger(__name__)

#: How far above the local floor a bin has to be before it counts as occupied. Six dB is twice
#: the power of the surrounding noise, which is above the swing of a quiet band's own variance
#: and below any carrier worth noticing.
DEFAULT_OCCUPANCY_DB = 6.0

#: Percentiles reported by the envelope mode. 5/50/95 rather than min/median/max because a
#: single-sample extreme is a glitch and these are not.
ENVELOPE_PERCENTILES: tuple[float, ...] = (5.0, 50.0, 95.0)


def estimate_floor(row: np.ndarray) -> float:
    """The noise floor of one row, robust to carriers sitting in it.

    The median rather than the mean, and that is the whole point: a row with a strong carrier
    in a few bins has a mean pulled up by it, while the median describes the noise most of the
    bins actually contain. On a band with a powerful signal the two differ by tens of dB, and
    the floor is the baseline everything else is measured against.
    """
    finite = row[np.isfinite(row)]
    return float(np.median(finite)) if finite.size else float("nan")


class HistoryView(NamedTuple):
    """What the history looks like right now, as arrays ready to plot."""

    peak: np.ndarray
    floor: float
    rows: int


@dataclass
class SpectralHistory:
    """Bounded history of rows, with the statistics the analysis modes need.

    Bounded on purpose. An unbounded history of a 60 rows/s stream would grow without limit on
    a display that is meant to run for hours; the window is what makes the statistics describe
    *recent* behaviour, and `capacity` is the knob that trades memory for memory length.
    """

    bins: int
    capacity: int = 600

    def __post_init__(self) -> None:
        if self.bins <= 0:
            raise ValueError("a history needs at least one bin")
        if self.capacity <= 0:
            raise ValueError("a history needs room for at least one row")
        self._peak = np.full(self.bins, -np.inf, dtype=np.float64)
        self._min = np.full(self.bins, np.inf, dtype=np.float64)
        self._rows: list[np.ndarray] = []
        self._occupancy = np.zeros(self.bins, dtype=np.int64)
        self._observed = 0
        self._rejected = 0

    # ── filling ────────────────────────────────────────────────────────────────
    def add(self, row: np.ndarray) -> bool:
        """Fold one row in. Returns False if it was the wrong shape and was ignored.

        Shape is checked rather than assumed: a row of the wrong length would either raise
        deep inside numpy or, worse, broadcast silently and poison every statistic in here
        with numbers from a different part of the band.
        """
        values = np.asarray(row, dtype=np.float64)
        if values.shape != (self.bins,):
            self._rejected += 1
            log.debug("Ignoring a row of shape %s, expected (%d,)", values.shape, self.bins)
            return False

        finite = np.isfinite(values)
        np.maximum(self._peak, np.where(finite, values, -np.inf), out=self._peak)
        np.minimum(self._min, np.where(finite, values, np.inf), out=self._min)

        floor = estimate_floor(values)
        above = finite & (values > floor + DEFAULT_OCCUPANCY_DB)
        self._occupancy += above

        self._rows.append(values)
        self._observed += 1
        if len(self._rows) > self.capacity:
            del self._rows[: len(self._rows) - self.capacity]
        return True

    def clear(self) -> None:
        """Forget everything. Statistics are for a session, not for all time."""
        self.__post_init__()

    # ── what it holds ──────────────────────────────────────────────────────────
    @property
    def rows(self) -> int:
        """Rows currently held — the window, not the total ever seen."""
        return len(self._rows)

    @property
    def observed(self) -> int:
        """Rows ever accepted, including ones since dropped from the window.

        Kept separately from `rows` because "how long has this been running" and "how much
        history is behind these numbers" are different questions, and a display that conflates
        them overstates how much evidence it has.
        """
        return self._observed

    @property
    def rejected(self) -> int:
        return self._rejected

    def has_data(self) -> bool:
        return bool(self._rows)

    def peak_hold(self) -> np.ndarray:
        return self._peak.copy()

    def recent_rows(self) -> np.ndarray:
        """The window as a `(rows, bins)` array, oldest first. Empty before any data.

        Returned as a fresh array rather than the live list, because the caller is usually a
        widget building a mesh from it, and handing out the internal buffer would let a redraw
        race the next row in — the same class of bug the survey accumulator had to fix.
        """
        if not self._rows:
            return np.zeros((0, self.bins), dtype=np.float64)
        return np.vstack(self._rows)

    def min_hold(self) -> np.ndarray:
        return self._min.copy()

    def floor_db(self) -> float:
        """The floor of the window as a whole, from the median of the per-row medians.

        Median of medians rather than the minimum of them: the quietest row in a window is
        usually a moment when the receiver happened to be between signals, and using it as the
        baseline would make every other row look occupied.
        """
        if not self._rows:
            return float("nan")
        return float(np.median([estimate_floor(row) for row in self._rows]))

    def percentiles(self, qs: tuple[float, ...] = ENVELOPE_PERCENTILES) -> dict[float, np.ndarray]:
        """Per-bin percentiles over the window."""
        if not self._rows:
            return {q: np.full(self.bins, np.nan) for q in qs}
        stacked = np.vstack(self._rows)
        return {q: np.percentile(stacked, q, axis=0) for q in qs}

    def occupancy(self) -> np.ndarray:
        """Fraction of rows where each bin was above that row's own floor.

        Counted over **every row ever accepted**, not just the ones still in the window — a
        duty cycle is about the whole session, and computing it from a rolling window would
        make it jitter as rows fell off the back. The distinction matters enough to name: the
        peak-hold and percentile figures describe the window, this one describes the run.
        """
        if not self._observed:
            return np.zeros(self.bins, dtype=np.float64)
        return self._occupancy / float(self._observed)


class Burst(NamedTuple):
    """One discrete event: something was above the floor for a while, over a span of bins."""

    start_row: int
    end_row: int
    first_bin: int
    last_bin: int
    peak_db: float
    floor_db: float

    @property
    def row_span(self) -> int:
        return self.end_row - self.start_row + 1

    @property
    def bin_span(self) -> int:
        return self.last_bin - self.first_bin + 1

    @property
    def over_floor_db(self) -> float:
        return self.peak_db - self.floor_db

    def describe(self, *, bin_hz: float, row_s: float) -> str:
        bandwidth_khz = self.bin_span * bin_hz / 1e3
        return (
            f"{self.over_floor_db:+.1f} dB over floor · {bandwidth_khz:.1f} kHz wide · "
            f"{self.row_span * row_s:.2f} s"
        )


@dataclass
class BurstDetector:
    """Turns a stream of rows into discrete events.

    A burst is contiguous in both senses: a run of rows, and within each of them a run of bins.
    Tracking both is what separates "a signal was here" from "something happened", which is the
    difference between a spectrum and a log.

    Within a row, all hot bins become **one** event rather than one per contiguous run. A
    modulated signal has notches in its spectrum, and treating each run as a separate event
    would report a single transmission as several — boundaries that look meaningful and are
    not.

    Events are only reported once they end, because a burst's duration and peak are not known
    until then. `flush()` is how a caller gets the one still in progress.
    """

    bins: int
    threshold_db: float = DEFAULT_OCCUPANCY_DB
    min_rows: int = 1

    def __post_init__(self) -> None:
        self._row_index = 0
        self._active = False
        self._start_row = 0
        self._last_row = 0
        self._first_bin = 0
        self._last_bin = 0
        self._peak = -np.inf
        self._floor = float("nan")

    def push(self, row: np.ndarray) -> list[Burst]:
        """Fold in a row, returning any event completed *by* it.

        A row with nothing hot closes whatever was open, so the return can be non-empty on the
        row that ended the burst rather than on the one that started it.
        """
        values = np.asarray(row, dtype=np.float64)
        if values.shape != (self.bins,):
            return []
        floor = estimate_floor(values)
        hot = np.isfinite(values) & (values > floor + self.threshold_db)

        if not hot.any():
            finished = self._close()
            self._row_index += 1
            return finished

        hot_bins = np.flatnonzero(hot)
        first, last = int(hot_bins[0]), int(hot_bins[-1])
        row_peak = float(values[hot].max())

        if not self._active:
            self._active = True
            self._start_row = self._row_index
            self._first_bin = first
            self._last_bin = last
            self._peak = row_peak
            self._floor = floor
        else:
            self._first_bin = min(self._first_bin, first)
            self._last_bin = max(self._last_bin, last)
            self._peak = max(self._peak, row_peak)

        self._last_row = self._row_index
        self._row_index += 1
        return []

    def _close(self) -> list[Burst]:
        """Finish the open event, discarding it if it was shorter than `min_rows`."""
        if not self._active:
            return []
        span = self._last_row - self._start_row + 1
        self._active = False
        if span < self.min_rows:
            return []
        return [Burst(
            start_row=self._start_row, end_row=self._last_row,
            first_bin=self._first_bin, last_bin=self._last_bin,
            peak_db=float(self._peak), floor_db=float(self._floor),
        )]

    def flush(self) -> list[Burst]:
        """Close and report the event still in progress, for when a capture ends mid-burst."""
        return self._close()

    @property
    def in_burst(self) -> bool:
        return self._active

    @property
    def rows_seen(self) -> int:
        return self._row_index


def slot_power_grid(
    rows: list[np.ndarray] | np.ndarray,
    *,
    centre_hz: float,
    span_hz: float,
    slot_width_hz: float = 250_000.0,
    max_slots: int = 64,
) -> tuple[np.ndarray, np.ndarray]:
    """Fold rows onto channel slots: `(grid, slot_centres_hz)`.

    The grid is slots x rows of mean power in dB, so a slot that carries traffic shows as a
    stripe down the time axis. This is the view that answers a question about the *mesh's*
    layout — which of its channels is in use — rather than about the spectrum.

    Slots narrower than a bin get the bin that contains them; the caller is expected to ask for
    a span and slot width that make sense together, and the returned centres are what the
    measurement could actually resolve rather than what was requested.
    """
    if not rows:
        return np.zeros((0, 0), dtype=np.float64), np.zeros(0, dtype=np.float64)
    stacked = np.vstack([np.asarray(r, dtype=np.float64) for r in rows])
    bin_count = stacked.shape[1]
    if bin_count == 0 or span_hz <= 0:
        return np.zeros((0, 0), dtype=np.float64), np.zeros(0, dtype=np.float64)

    bin_hz = span_hz / bin_count
    low_hz = centre_hz - span_hz / 2
    slot_count = max(1, min(max_slots, int(span_hz // slot_width_hz)))
    centres = low_hz + (np.arange(slot_count) + 0.5) * (span_hz / slot_count)

    grid = np.full((slot_count, stacked.shape[0]), np.nan, dtype=np.float64)
    for index in range(slot_count):
        lo = low_hz + index * (span_hz / slot_count)
        hi = lo + span_hz / slot_count
        first = int(np.floor((lo - low_hz) / bin_hz))
        last = int(np.ceil((hi - low_hz) / bin_hz))
        first = max(0, min(first, bin_count - 1))
        last = max(first + 1, min(last, bin_count))
        block = stacked[:, first:last]
        # Sum and count rather than nanmean: nanmean on a slice with nothing measured emits a
        # "mean of empty slice" warning and returns NaN anyway. Doing it explicitly keeps the
        # same honest answer — a slot nobody measured reads as unknown, never as zero — with
        # no warning to be filtered out at the call site.
        measured = np.isfinite(block).sum(axis=1)
        totals = np.nansum(block, axis=1)
        grid[index, :] = np.where(measured > 0, totals / np.maximum(measured, 1), np.nan)
    return grid, centres
