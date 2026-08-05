"""MeshChat – analytics: signal metric calculations."""
from __future__ import annotations

import statistics
from typing import NamedTuple


class SignalStats(NamedTuple):
    median_snr: float | None
    median_rssi: float | None
    mean_snr: float | None
    mean_rssi: float | None
    min_snr: float | None
    max_snr: float | None
    min_rssi: float | None
    max_rssi: float | None
    sample_count: int


def compute_signal_stats(
    snr_values: list[float],
    rssi_values: list[int],
) -> SignalStats:
    """
    Compute rolling signal statistics from lists of SNR and RSSI samples.
    Medians are used as main values (resist outliers better than means).
    """
    snr = [v for v in snr_values if v is not None]
    rssi = [v for v in rssi_values if v is not None]

    def safe_median(lst: list) -> float | None:
        return statistics.median(lst) if lst else None

    def safe_mean(lst: list) -> float | None:
        return statistics.mean(lst) if lst else None

    return SignalStats(
        median_snr=safe_median(snr),
        median_rssi=safe_median(rssi),
        mean_snr=safe_mean(snr),
        mean_rssi=safe_mean(rssi),
        min_snr=min(snr) if snr else None,
        max_snr=max(snr) if snr else None,
        min_rssi=min(rssi) if rssi else None,
        max_rssi=max(rssi) if rssi else None,
        sample_count=max(len(snr), len(rssi)),
    )


def format_snr(snr: float | None) -> str:
    if snr is None:
        return "N/A"
    return f"{snr:+.1f} dB"


def format_rssi(rssi: float | None) -> str:
    if rssi is None:
        return "N/A"
    return f"{rssi:.0f} dBm"
