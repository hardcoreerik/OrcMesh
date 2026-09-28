"""Region sizing for the setup wizard — estimate before, measure after.

A user picking their county has to be told what it will cost them *before*
anything is downloaded or built. Neither OrcMaps tool reports a prospective
size (`provision_pack.py --dry-run` prints its plan with no size, and only sets
``size_bytes`` once the archive exists), so the estimate has to live here.

Everything in this module is derived from measured builds, and every constant
carries the measurement it came from, because the honest error bar matters more
than a tidy number. The measured points we have:

* **Oregon z1-13** = 84,615,534 B over 254,800 km2 = **332 B/km2**, built in
  80 s (OrcMaps ``docs/GLOBAL_Z13_COVERAGE_ANALYSIS.md``).
* **Oregon z0-9** = 8.36 MB over the same area = **32.8 B/km2**, measured on
  this machine. So dropping the deepest four zoom levels is ~10x smaller.
* **Pin cuts from the 84 MB Oregon source** (OrcMaps ``tools/pack-builder/README.md``):
  25 km -> 4.06 MB, 50 km -> 7.34 MB, 100 km -> 20.30 MB.
* **Oregon -> United States extrapolation failure**: scaling Oregon's density to
  US land area predicted 153 MB; the real z0-9 build was **261 MB**, i.e. 1.7x
  higher, because dense states exceed a mostly-rural state's average. That is
  why the band below is wide rather than tight.

Two consequences are encoded in the API: an estimate is always a *range* with a
stated basis, and a small local cut is estimated from the measured pin table
rather than from an area average, because averaging over a state badly
underestimates a dense urban cut.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

#: Bytes per km2 at z<=13, from OrcMaps' measured Oregon z1-13 build.
DENSITY_BYTES_PER_KM2_Z13 = 332.0

#: Bytes per km2 at z<=9, measured on this machine (Oregon z0-9 = 8.36 MB).
DENSITY_BYTES_PER_KM2_Z9 = 32.8

#: Measured pin cuts (radius_km, size_bytes) from an 84 MB z1-13 source.
MEASURED_PIN_CUTS: tuple[tuple[float, int], ...] = (
    (25.0, 4_060_000),
    (50.0, 7_340_000),
    (100.0, 20_300_000),
)

#: Ratio between the z9 and z13 densities over four zoom levels, used to scale
#: an estimate when the source archive stops somewhere in between.
_ZOOM_SCALE_PER_LEVEL = (DENSITY_BYTES_PER_KM2_Z13 / DENSITY_BYTES_PER_KM2_Z9) ** (1 / 4)

#: How far the truth can sit from `expected`. Calibrated on a real miss: Oregon
#: density predicted 153 MB for the US z0-9 pack and the build landed at 261 MB
#: (1.7x). Half to double brackets that and the sparse-region direction too.
_BAND_LOW = 0.5
_BAND_HIGH = 2.0

_KM_PER_DEGREE_LAT = 111.32

#: Default zoom ceilings per tier, mirroring OrcMaps' three-tier card example
#: (world z0-7, regional z1-13, local z0-15). A source archive may cap lower,
#: which is the caller's problem to clamp.
TIER_MAX_ZOOM: dict[str, int] = {"region": 13, "local": 15}

TIER_LABELS: dict[str, str] = {
    "region": "Region — your county or area",
    "local": "Local detail — your town",
}


@dataclass(frozen=True)
class PackSizeEstimate:
    """A size range with the reasoning attached.

    ``basis`` is shown to the user verbatim: an estimate whose origin is hidden
    is indistinguishable from a guess, and this wizard asks people to spend
    bandwidth and disk on it.
    """

    low: int
    expected: int
    high: int
    basis: str

    def format_expected(self) -> str:
        return f"{self.expected / 1e6:.0f} MB"

    def format_range(self) -> str:
        return f"{self.low / 1e6:.0f}–{self.high / 1e6:.0f} MB"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def zoom_scale(max_zoom: int, reference_zoom: int = 13) -> float:
    """Scale factor for a pack whose ceiling differs from the reference build.

    Each additional zoom level roughly quadruples the tile count, and the
    measured z9 -> z13 difference (32.8 -> 332 B/km2, ~10x over four levels)
    gives ~1.9x per level. Beyond the measured range this is extrapolation, and
    the caller's `basis` text should say so.
    """
    return _ZOOM_SCALE_PER_LEVEL ** (max_zoom - reference_zoom)


def bbox_area_km2(west: float, south: float, east: float, north: float) -> float:
    """Area of a lat/lon rectangle, approximated on a sphere.

    Good enough for a size estimate: the error from a flat-earth approximation
    at these scales is far smaller than the density variation the estimate is
    already admitting to.
    """
    height = abs(north - south) * _KM_PER_DEGREE_LAT
    mean_lat = math.radians((north + south) / 2)
    width = abs(east - west) * _KM_PER_DEGREE_LAT * abs(math.cos(mean_lat))
    return width * height


def estimate_radius_pack_bytes(radius_km: float, max_zoom: int = 13) -> PackSizeEstimate:
    """Estimate a pin-style cut from the measured pin table.

    Interpolated in log-log space between the measured radii, and clamped to the
    measured gradient outside them. The table comes from an 84 MB z1-13 source,
    so a different ceiling scales it via `zoom_scale`.
    """
    radius_km = max(radius_km, 0.5)
    points = MEASURED_PIN_CUTS

    def _interp(value: float) -> float:
        if value <= points[0][0]:
            lower, upper = points[0], points[1]
        elif value >= points[-1][0]:
            lower, upper = points[-2], points[-1]
        else:
            lower, upper = points[0], points[-1]
            for index in range(len(points) - 1):
                if points[index][0] <= value <= points[index + 1][0]:
                    lower, upper = points[index], points[index + 1]
                    break
        span = math.log(upper[0] / lower[0])
        if span == 0:
            return float(lower[1])
        fraction = math.log(value / lower[0]) / span
        return math.exp(
            math.log(lower[1]) + fraction * math.log(upper[1] / lower[1])
        )

    expected = _interp(radius_km) * zoom_scale(max_zoom)
    basis = (
        f"Interpolated from measured {points[0][0]:.0f}/{points[1][0]:.0f}/"
        f"{points[-1][0]:.0f} km cuts of an 84 MB z1-13 source "
        f"({points[0][1] / 1e6:.1f}/{points[1][1] / 1e6:.1f}/"
        f"{points[-1][1] / 1e6:.1f} MB)"
    )
    if max_zoom != 13:
        basis += f", scaled for z<={max_zoom}"
    return PackSizeEstimate(
        low=round(expected * _BAND_LOW),
        expected=round(expected),
        high=round(expected * _BAND_HIGH),
        basis=basis,
    )


def estimate_area_pack_bytes(area_km2: float, max_zoom: int = 13) -> PackSizeEstimate:
    """Estimate a large region from measured density.

    Uses the z1-13 density measured on Oregon and scales it for a different
    ceiling. Deliberately not used for small dense cuts — see the module
    docstring on why an area average underestimates a city.
    """
    density = DENSITY_BYTES_PER_KM2_Z13 * zoom_scale(max_zoom)
    expected = max(area_km2, 1.0) * density
    basis = (
        f"Measured density {DENSITY_BYTES_PER_KM2_Z13:.0f} B/km² at z<=13 "
        f"(Oregon: 84.6 MB over 254,800 km²) applied to {area_km2:,.0f} km²"
    )
    if max_zoom != 13:
        basis += f", scaled for z<={max_zoom}"
    return PackSizeEstimate(
        low=round(expected * _BAND_LOW),
        expected=round(expected),
        high=round(expected * _BAND_HIGH),
        basis=basis,
    )


def estimate_region_bytes(
    *,
    radius_km: float | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    max_zoom: int = 13,
) -> PackSizeEstimate:
    """Estimate either shape of region; exactly one input must be given.

    A radius uses the measured pin table, a bbox uses density — the two are
    kept apart on purpose, because the pin table already encodes the fact that
    a small dense cut costs far more per km² than a state average.
    """
    if (radius_km is None) == (bbox is None):
        raise ValueError("give exactly one of radius_km or bbox")
    if radius_km is not None:
        return estimate_radius_pack_bytes(radius_km, max_zoom)
    assert bbox is not None
    return estimate_area_pack_bytes(bbox_area_km2(*bbox), max_zoom)
