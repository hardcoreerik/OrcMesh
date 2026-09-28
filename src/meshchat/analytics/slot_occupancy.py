"""Rank how much energy each mesh channel slot actually holds.

A band scan answers "where is there energy in 902-928 MHz". This turns that into
the question a user actually has: *which of my mesh's channel slots is carrying
traffic?* Slots come from the same band plan the Spectrum page draws its markers
from (`analytics.lora_bands`), so a slot highlighted on the waterfall and a row in
this ranking are the same thing.

Deliberately independent of the SDR layer: it takes a frequency axis and a power
array rather than a scan object, which keeps the analysis testable with synthetic
bands and reusable for a recorded or replayed scan.

**Resolution limit worth knowing.** These rankings are only as sharp as the scan's
bin spacing. rtl_power picks its own bin size from the sample rate, and over
902-928 MHz that lands at 81.25 kHz — against a 125 kHz LoRa slot it is roughly
one to two bins per slot, so neighbouring slots bleed into each other and a slot
boundary cannot be found precisely. Ask for a bin size well below the slot width
(20 kHz, say) when slot-level detail is the point; the third field of rtl_power's
``-f`` is a maximum, and it will use finer bins when the geometry allows.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from meshchat.analytics.lora_bands import ChannelMarker

#: A peak within this many dB of the band's floor reads as "nothing here". LoRa
#: bursts sit tens of dB above the floor at the gains these scans use, so a small
#: margin separates an idle slot from a busy one without needing calibration.
IDLE_MARGIN_DB = 6.0

#: The noise floor comes from a low percentile rather than the median: in a band
#: carrying several strong signals the median is dragged upward by them, which
#: would make every slot look quiet.
_FLOOR_PERCENTILE = 25.0

#: Slot states. "unknown" exists because a slot that was never measured must not
#: be reported as idle — that would be claiming knowledge the scan does not have.
STATUS_BUSY = "busy"
STATUS_IDLE = "idle"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class SlotOccupancy:
    """What one channel slot looks like in a scanned band."""

    label: str
    center_hz: float
    bandwidth_hz: float
    bins: int
    mean_db: float
    peak_db: float
    noise_floor_db: float
    in_scan: bool

    @property
    def has_data(self) -> bool:
        """True when at least one bin inside the slot was actually measured."""
        return self.bins > 0

    @property
    def peak_over_floor_db(self) -> float:
        """How far the slot's strongest bin stands above the band's floor.

        Peak rather than mean: a LoRa transmission is intermittent and narrow, so
        averaging across a whole slot dilutes a burst that the peak still shows.
        """
        if not self.has_data:
            return float("nan")
        return self.peak_db - self.noise_floor_db

    @property
    def mean_over_floor_db(self) -> float:
        if not self.has_data:
            return float("nan")
        return self.mean_db - self.noise_floor_db

    @property
    def status(self) -> str:
        """``busy``, ``idle``, or ``unknown`` — unknown is not the same as idle."""
        if not self.has_data:
            return STATUS_UNKNOWN
        if self.peak_over_floor_db < IDLE_MARGIN_DB:
            return STATUS_IDLE
        return STATUS_BUSY

    @property
    def looks_busy(self) -> bool:
        return self.status == STATUS_BUSY


@dataclass(frozen=True)
class OccupancyReport:
    """Every slot, hottest first; slots that could not be measured come last."""

    slots: tuple[SlotOccupancy, ...]
    noise_floor_db: float
    scanned_low_hz: float
    scanned_high_hz: float

    @property
    def hottest(self) -> SlotOccupancy | None:
        """The busiest measured slot, or None when nothing could be ranked."""
        return next((slot for slot in self.slots if slot.looks_busy), None)

    @property
    def measured_slots(self) -> int:
        return sum(1 for slot in self.slots if slot.has_data)

    @property
    def unknown_slots(self) -> int:
        return sum(1 for slot in self.slots if not slot.has_data)

    @property
    def busy_slots(self) -> int:
        return sum(1 for slot in self.slots if slot.looks_busy)


def _rank_key(slot: SlotOccupancy) -> tuple[int, float, str]:
    """Hottest first; unmeasured last.

    The first element keeps unmeasured slots out of the numeric comparison
    entirely, so a slot with no data can never be sorted as though its value
    were zero — which would place it among the quietest rather than as unknown.
    The label breaks ties so the order is stable between sweeps.
    """
    if not slot.has_data:
        return (1, 0.0, slot.label)
    return (0, -slot.peak_over_floor_db, slot.label)


def rank_slots(
    frequencies_hz: np.ndarray,
    power_db: np.ndarray,
    markers: Sequence[ChannelMarker],
    *,
    floor_percentile: float = _FLOOR_PERCENTILE,
) -> OccupancyReport:
    """Measure each marker's slot in a scanned band and rank them.

    `frequencies_hz` and `power_db` are parallel arrays from one scan; NaN power
    (a bin the tool could not measure) is skipped rather than counted as signal
    or as silence.
    """
    frequencies = np.asarray(frequencies_hz, dtype=np.float64)
    power = np.asarray(power_db, dtype=np.float64)
    if frequencies.size != power.size:
        raise ValueError(
            f"frequencies and power must be the same length, got "
            f"{frequencies.size} and {power.size}"
        )

    measured = np.isfinite(power)
    floor_db = (
        float(np.percentile(power[measured], floor_percentile))
        if measured.any()
        else float("nan")
    )

    if frequencies.size:
        low_hz = float(frequencies.min())
        high_hz = float(frequencies.max())
    else:
        low_hz = high_hz = float("nan")

    slots: list[SlotOccupancy] = []
    for marker in markers:
        center_hz = marker.center_mhz * 1e6
        bandwidth_hz = marker.bandwidth_khz * 1e3
        half = bandwidth_hz / 2
        inside = (frequencies >= center_hz - half) & (frequencies <= center_hz + half)
        values = power[inside & measured]
        slots.append(
            SlotOccupancy(
                label=marker.label,
                center_hz=center_hz,
                bandwidth_hz=bandwidth_hz,
                bins=int(values.size),
                mean_db=float(values.mean()) if values.size else float("nan"),
                peak_db=float(values.max()) if values.size else float("nan"),
                noise_floor_db=floor_db,
                in_scan=bool(frequencies.size and low_hz <= center_hz <= high_hz),
            )
        )

    return OccupancyReport(
        slots=tuple(sorted(slots, key=_rank_key)),
        noise_floor_db=floor_db,
        scanned_low_hz=low_hz,
        scanned_high_hz=high_hz,
    )
