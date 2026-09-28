"""Remembering how the SIGINT view was last set up.

Every launch used to start from the preset's defaults, which is right for a first run and wrong
for the tenth: an operator working a specific band re-enters the same centre, rate and gain
every time, and a tool that forgets is one they work around rather than with.

Kept out of the page so the rules are testable without a window, and kept deliberately small —
this stores what was *typed*, not what was *measured*. Anything the receiver reported stays out
of it, because a saved gain that the hardware refused is a setting that will look applied and
not be.

**Nothing here is trusted back.** A stored value is a value from another session, possibly from
another machine, and possibly edited by hand. Every field is validated against the same ranges
the controls enforce, and anything out of range is dropped in favour of the default rather than
clamped: a centre frequency silently moved by a hundred megahertz is worse than one that was
obviously not restored.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

_PREFIX = "sigint."

#: The range the centre control offers, in MHz, and the rate and gain ranges alongside it.
MIN_CENTRE_MHZ, MAX_CENTRE_MHZ = 0.024, 1766.0
MIN_RATE_MSPS, MAX_RATE_MSPS = 0.25, 3.2
MIN_GAIN_DB, MAX_GAIN_DB = 0.0, 49.6


class SettingStore(Protocol):
    """The two calls this needs, so it can be tested with a dictionary."""

    def get_setting(self, key: str) -> str | None: ...
    def set_setting(self, key: str, value: str) -> None: ...


@dataclass(frozen=True)
class SigintTuning:
    """The controls the operator sets, as they were left."""

    centre_mhz: float
    rate_msps: float
    gain_db: float
    region: str = ""
    preset: str = ""
    device_index: int = 0

    @classmethod
    def load(cls, store: SettingStore) -> SigintTuning | None:
        """Read the last tuning, or None if there is none usable.

        None rather than a default-filled object, so a caller can tell "never set up before"
        from "set up and stored these values" and choose its own defaults for the first case.
        """
        centre = _read_float(store, "centre_mhz", MIN_CENTRE_MHZ, MAX_CENTRE_MHZ)
        rate = _read_float(store, "rate_msps", MIN_RATE_MSPS, MAX_RATE_MSPS)
        gain = _read_float(store, "gain_db", MIN_GAIN_DB, MAX_GAIN_DB)
        if centre is None or rate is None or gain is None:
            # Partial state is not restored at all. Restoring half of a tuning would put the
            # receiver somewhere neither the last session nor the defaults intended.
            log.debug("Ignoring an incomplete stored SIGINT tuning")
            return None
        return cls(
            centre_mhz=centre,
            rate_msps=rate,
            gain_db=gain,
            region=_read_text(store, "region"),
            preset=_read_text(store, "preset"),
            device_index=_read_int(store, "device_index", 0, 16),
        )

    def save(self, store: SettingStore) -> None:
        """Write the tuning back. Silently does nothing if the store refuses."""
        pairs = {
            "centre_mhz": f"{self.centre_mhz:.6f}",
            "rate_msps": f"{self.rate_msps:.6f}",
            "gain_db": f"{self.gain_db:.2f}",
            "region": self.region,
            "preset": self.preset,
            "device_index": str(self.device_index),
        }
        for key, value in pairs.items():
            try:
                store.set_setting(f"{_PREFIX}{key}", value)
            except Exception:  # pragma: no cover - a settings write must never break a capture
                log.warning("Could not store %s%s", _PREFIX, key, exc_info=True)
                return

    def describe(self) -> str:
        return (
            f"{self.centre_mhz:.3f} MHz at {self.rate_msps:.2f} MS/s, "
            f"{self.gain_db:.1f} dB gain"
        )


def _read_float(store: SettingStore, key: str, low: float, high: float) -> float | None:
    raw = store.get_setting(f"{_PREFIX}{key}")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        log.debug("Stored %s%s is not a number: %r", _PREFIX, key, raw)
        return None
    if not low <= value <= high:
        log.info("Stored %s%s of %s is outside %s..%s; ignoring it", _PREFIX, key, value, low, high)
        return None
    return value


def _read_int(store: SettingStore, key: str, low: int, high: int) -> int:
    raw = store.get_setting(f"{_PREFIX}{key}")
    try:
        value = int(raw) if raw else low
    except ValueError:
        return low
    return value if low <= value <= high else low


def _read_text(store: SettingStore, key: str) -> str:
    return store.get_setting(f"{_PREFIX}{key}") or ""
