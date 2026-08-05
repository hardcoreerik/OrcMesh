"""MeshChat – analytics: Haversine distance calculations."""
from __future__ import annotations

import math


_EARTH_RADIUS_KM = 6371.0


def haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """
    Return distance in kilometres between two WGS-84 coordinates.
    Uses the Haversine formula.
    """
    φ1 = math.radians(lat1)
    φ2 = math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)

    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_KM * c


def haversine_mi(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Return distance in miles."""
    return haversine_km(lat1, lon1, lat2, lon2) * 0.621371


def bearing_deg(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """
    Return initial bearing in degrees (0-360) from point 1 to point 2.
    """
    φ1 = math.radians(lat1)
    φ2 = math.radians(lat2)
    Δλ = math.radians(lon2 - lon1)

    x = math.sin(Δλ) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(Δλ)
    θ = math.atan2(x, y)
    return (math.degrees(θ) + 360) % 360


def format_distance(km: float, use_metric: bool = True) -> str:
    """Format a distance for display."""
    if use_metric:
        if km < 1.0:
            return f"{km * 1000:.0f} m"
        return f"{km:.2f} km"
    else:
        mi = km * 0.621371
        if mi < 0.1:
            ft = km * 3280.84
            return f"{ft:.0f} ft"
        return f"{mi:.2f} mi"
