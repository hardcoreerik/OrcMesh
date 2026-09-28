"""MeshChat – LoRa band plans for Meshtastic and MeshCore.

Turns a region + modem preset into the actual RF frequencies a mesh occupies,
so the spectrum view can mark where to look instead of showing a bare band.

Frequency-slot math mirrors the Meshtastic firmware:

    freq = freq_start + (bw_khz / 2000) + (channel_num * bw_khz / 1000)   [MHz]

MeshCore uses a single fixed frequency per region rather than a slot plan.

These are derived from published firmware defaults. Treat them as a guide for
where to point the SDR, not as regulatory authority — confirm the rules that
apply to you before transmitting on any of it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BandRange:
    """An allocation a mesh protocol operates within."""
    name: str
    start_mhz: float
    end_mhz: float

    @property
    def center_mhz(self) -> float:
        return (self.start_mhz + self.end_mhz) / 2

    @property
    def span_mhz(self) -> float:
        return self.end_mhz - self.start_mhz


@dataclass(frozen=True)
class ChannelMarker:
    """One occupied slot to draw on the spectrum display."""
    label: str
    center_mhz: float
    bandwidth_khz: float


# ── Meshtastic region allocations ──────────────────────────────────────────
# Keyed by the RegionCode enum name reported by the radio.
MESHTASTIC_REGIONS: dict[str, BandRange] = {
    "US":      BandRange("US 902-928",      902.0,  928.0),
    "EU_433":  BandRange("EU 433",          433.0,  434.0),
    "EU_868":  BandRange("EU 868",          869.4,  869.65),
    "CN":      BandRange("CN 470-510",      470.0,  510.0),
    "JP":      BandRange("JP 920-928",      920.8,  927.8),
    "ANZ":     BandRange("ANZ 915-928",     915.0,  928.0),
    "ANZ_433": BandRange("ANZ 433",         433.05, 434.79),
    "KR":      BandRange("KR 920-923",      920.0,  923.0),
    "TW":      BandRange("TW 920-925",      920.0,  925.0),
    "RU":      BandRange("RU 868",          868.7,  869.2),
    "IN":      BandRange("IN 865-867",      865.0,  867.0),
    "NZ_865":  BandRange("NZ 865",          864.0,  868.0),
    "TH":      BandRange("TH 920-925",      920.0,  925.0),
    "UA_433":  BandRange("UA 433",          433.0,  434.7),
    "UA_868":  BandRange("UA 868",          868.0,  868.6),
    "MY_433":  BandRange("MY 433",          433.0,  435.0),
    "MY_919":  BandRange("MY 919-924",      919.0,  924.0),
    "SG_923":  BandRange("SG 917-925",      917.0,  925.0),
    "LORA_24": BandRange("2.4 GHz",        2400.0, 2483.5),
}

# ── Meshtastic modem presets → (bandwidth kHz, spreading factor) ───────────
MESHTASTIC_PRESETS: dict[str, tuple[float, int]] = {
    "SHORT_TURBO":    (500.0, 7),
    "SHORT_FAST":     (250.0, 7),
    "SHORT_SLOW":     (250.0, 8),
    "MEDIUM_FAST":    (250.0, 9),
    "MEDIUM_SLOW":    (250.0, 10),
    "LONG_FAST":      (250.0, 11),
    "LONG_MODERATE":  (125.0, 11),
    "LONG_SLOW":      (125.0, 12),
    "VERY_LONG_SLOW": (62.5,  12),
}

# ── Single-channel LoRa networks ───────────────────────────────────────────
# MeshCore and Reticulum have no slot plan: an operator picks one frequency (or one
# per region) and the whole network sits on it. Everything downstream that expects a
# "plan" of one channel can share this shape.
@dataclass(frozen=True)
class SingleChannelPlan:
    region: str
    freq_mhz: float
    bandwidth_khz: float
    spreading_factor: int


# Kept as an alias: the name this table was introduced under.
MeshCorePlan = SingleChannelPlan


MESHCORE_PLANS: dict[str, SingleChannelPlan] = {
    "US":     SingleChannelPlan("US / ANZ 915", 910.525, 250.0, 11),
    "EU":     SingleChannelPlan("EU 869",       869.525, 250.0, 11),
    "UK":     SingleChannelPlan("UK 869",       869.525, 250.0, 11),
    "AU_NZ":  SingleChannelPlan("AU / NZ 915",  915.525, 250.0, 11),
    "IN":     SingleChannelPlan("IN 866",       865.525, 250.0, 11),
    "JP":     SingleChannelPlan("JP 923",       923.250, 250.0, 11),
    "433":    SingleChannelPlan("433 ISM",      433.500, 250.0, 11),
}

# ── Reticulum (RNode) ──────────────────────────────────────────────────────
# Reticulum deliberately has no band plan: an RNode is configured with a single
# frequency and the operator chooses it for their region and licence. What is fixed
# is the modem configuration RNode documents — 125 kHz, SF8, CR 4/5 — and the shared
# ISM allocation each region puts it in. So these entries are a *starting point*:
# they tune where RNode traffic is normally found, and the frequency the network is
# actually on lives in that network's own Reticulum config.
RETICULUM_PLANS: dict[str, SingleChannelPlan] = {
    "US":     SingleChannelPlan("US 915 ISM",  915.000, 125.0, 8),
    "EU":     SingleChannelPlan("EU 868 ISM",  867.200, 125.0, 8),
    "UK":     SingleChannelPlan("UK 869 ISM",  869.525, 125.0, 8),
    "AU_NZ":  SingleChannelPlan("AU / NZ 915", 915.000, 125.0, 8),
    "IN":     SingleChannelPlan("IN 865 ISM",  865.600, 125.0, 8),
    "JP":     SingleChannelPlan("JP 920 ISM",  920.800, 125.0, 8),
    "433":    SingleChannelPlan("433 ISM",      433.500, 125.0, 8),
}


# ── Meshtastic slot math ───────────────────────────────────────────────────

def meshtastic_num_channels(region: str, bandwidth_khz: float) -> int:
    """How many slots of this width fit in the region's allocation."""
    band = MESHTASTIC_REGIONS.get(region)
    if band is None or bandwidth_khz <= 0:
        return 0
    return max(int(band.span_mhz / (bandwidth_khz / 1000.0)), 1)


def meshtastic_channel_frequency(
    region: str, bandwidth_khz: float, channel_num: int
) -> float | None:
    """Centre frequency (MHz) of a Meshtastic channel slot."""
    band = MESHTASTIC_REGIONS.get(region)
    if band is None:
        return None
    count = meshtastic_num_channels(region, bandwidth_khz)
    if count == 0:
        return None
    slot = channel_num % count
    return band.start_mhz + (bandwidth_khz / 2000.0) + (slot * bandwidth_khz / 1000.0)


def meshtastic_markers(
    region: str,
    preset: str | None,
    channel_num: int,
    *,
    include_neighbours: int = 0,
    slot_label: str | None = None,
) -> list[ChannelMarker]:
    """Marker(s) for where a Meshtastic mesh sits in the band.

    `include_neighbours` also marks N slots either side, which is useful for
    seeing whether nearby meshes are using adjacent slots.

    `slot_label` overrides what the central slot is called. A caller that has
    chosen a slot for a *starting point* rather than read one off a radio needs
    this: the default label says "active", which would be a claim the caller
    cannot make.

    Caveat on channel_num == 0: the radio reports 0 to mean "let the firmware
    pick", and the firmware then derives the slot by hashing the primary
    channel's name. We deliberately do not reimplement that hash — getting it
    subtly wrong would put the marker on a confidently incorrect frequency.
    Slot 0 is marked as *nominal* instead; trust the energy in the waterfall
    over the marker if the two disagree.
    """
    bw_khz, _sf = MESHTASTIC_PRESETS.get(preset or "", (250.0, 11))
    auto_slot = channel_num == 0

    markers: list[ChannelMarker] = []
    for offset in range(-include_neighbours, include_neighbours + 1):
        freq = meshtastic_channel_frequency(region, bw_khz, channel_num + offset)
        if freq is None:
            continue
        if offset:
            label = f"Ch {channel_num + offset}"
        elif auto_slot:
            label = "Ch 0 (nominal — firmware auto-selects)"
        else:
            label = slot_label or f"Ch {channel_num} (active)"
        markers.append(ChannelMarker(label, freq, bw_khz))
    return markers


def meshcore_markers(region_key: str) -> list[ChannelMarker]:
    plan = MESHCORE_PLANS.get(region_key)
    if plan is None:
        return []
    return [ChannelMarker(f"MeshCore {plan.region}", plan.freq_mhz, plan.bandwidth_khz)]


def reticulum_markers(region_key: str) -> list[ChannelMarker]:
    """Where RNode traffic normally sits. A starting point, not a schedule — see
    RETICULUM_PLANS: the real frequency is in the network's own config."""
    plan = RETICULUM_PLANS.get(region_key)
    if plan is None:
        return []
    return [ChannelMarker(f"Reticulum {plan.region}", plan.freq_mhz, plan.bandwidth_khz)]
