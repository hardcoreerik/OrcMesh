"""Named set-ups that put the spectrum and waterfall on a LoRa network.

Choosing a preset is meant to answer the whole question at once — where to tune, how
wide to open the window, what gain to run, and which channels to mark — so "capture
LoRa for Meshtastic" is one selection instead of four controls and a mental note of
which slot you meant.

The band maths is deliberately not here. Every frequency comes out of `lora_bands`,
so a preset cannot disagree with the slot ranking and the channel menu drawn from the
same tables. This module only decides which of those numbers make a good capture, and
explains why in the preset's own note.

Display range is not here either, on purpose: the 2D waterfall fits its own dB window
to what is arriving and the 3D surface anchors to the measured noise floor, so a
preset carrying a level setting would fight both of them.
"""
from __future__ import annotations

from dataclasses import dataclass

from meshchat.analytics.lora_bands import (
    MESHTASTIC_PRESETS,
    ChannelMarker,
    meshtastic_channel_frequency,
    meshtastic_markers,
    meshtastic_num_channels,
    reticulum_markers,
    meshcore_markers,
)

#: Every preset opens the same span. 2.0 MS/s is the rate verified clean on this
#: hardware — rtl_sdr reports 2000000.05 Hz and wrote 8,000,000 bytes over 2 s with
#: no dropped-sample warning — and 1024 bins across 2 MHz is 1.95 kHz per bin, which
#: is 64 bins across a 125 kHz RNode channel and 128 across a 250 kHz Meshtastic one.
#: A narrower window would buy resolution the modulator never uses.
_PRESET_SPAN_MHZ = 2.0

#: The gain presets ask for. It is the app's own default rather than a preset's
#: opinion, and it is a measured choice: auto gain resolves to near-maximum on this
#: dongle, which put the noise floor at +17 dB against -17 dB at 3.7 dB. A LoRa
#: signal is only 10-20 dB over the floor at useful range, so headroom is the thing
#: worth having.
_PRESET_GAIN_DB = 16.0

#: Slot the US preset starts from. 906.875 MHz at 250 kHz, i.e. slot 19.
_US_SLOT = 19


@dataclass(frozen=True)
class SdrPreset:
    """One complete capture set-up for one kind of network."""

    key: str
    technology: str
    label: str
    center_mhz: float
    span_mhz: float
    gain_db: float
    note: str
    markers: tuple[ChannelMarker, ...] = ()
    #: The MESHTASTIC_REGIONS key this sits in, when there is one. Only Meshtastic
    #: presets have it: the region menu drives the slot ranking, which means nothing
    #: for a single-channel network like MeshCore or Reticulum.
    region: str | None = None

    def describe(self) -> str:
        """One line for the status bar: what is now on screen."""
        return (
            f"{self.label} — {self.center_mhz:.4f} MHz ± {self.span_mhz / 2:.2f} MHz, "
            f"gain {self.gain_db:g} dB"
        )


# ── Builders ────────────────────────────────────────────────────────────────
# Two shapes, because the networks come in two shapes: a region with many slots to
# park on, and a region so narrow the whole allocation is the picture.


def _slot_preset(
    *,
    key: str,
    label: str,
    region: str,
    modem: str,
    slot: int,
    neighbours: int,
    slot_label: str,
    note: str,
) -> SdrPreset:
    """Centre on one channel slot of a wide region, marking its neighbours."""
    bandwidth_khz, _sf = MESHTASTIC_PRESETS[modem]
    center = meshtastic_channel_frequency(region, bandwidth_khz, slot)
    if center is None:
        raise ValueError(f"{region} has no channel plan for {modem}")
    markers = meshtastic_markers(
        region, modem, slot, include_neighbours=neighbours, slot_label=slot_label
    )
    return SdrPreset(
        key=key,
        technology="Meshtastic",
        label=label,
        center_mhz=center,
        span_mhz=_PRESET_SPAN_MHZ,
        gain_db=_PRESET_GAIN_DB,
        note=note,
        markers=tuple(markers),
        region=region,
    )


def _allocation_preset(
    *, key: str, label: str, region: str, modem: str, note: str
) -> SdrPreset:
    """Centre on a whole narrow allocation and mark every slot in it."""
    bandwidth_khz, _sf = MESHTASTIC_PRESETS[modem]
    count = meshtastic_num_channels(region, bandwidth_khz)
    markers = tuple(
        ChannelMarker(
            f"Ch {index}",
            meshtastic_channel_frequency(region, bandwidth_khz, index) or 0.0,
            bandwidth_khz,
        )
        for index in range(count)
    )
    if not markers:
        raise ValueError(f"{region} has no channel plan for {modem}")
    centre = sum(m.center_mhz for m in markers) / len(markers)
    return SdrPreset(
        key=key,
        technology="Meshtastic",
        label=label,
        center_mhz=centre,
        span_mhz=_PRESET_SPAN_MHZ,
        gain_db=_PRESET_GAIN_DB,
        note=note,
        markers=markers,
        region=region,
    )


def _single_channel_preset(
    *, key: str, label: str, technology: str, region: str, note: str
) -> SdrPreset:
    """A network that parks on one frequency, in whatever region it runs in."""
    markers = (
        meshcore_markers(region) if technology == "MeshCore" else reticulum_markers(region)
    )
    if not markers:
        raise ValueError(f"no {technology} plan for {region}")
    return SdrPreset(
        key=key,
        technology=technology,
        label=label,
        center_mhz=markers[0].center_mhz,
        span_mhz=_PRESET_SPAN_MHZ,
        gain_db=_PRESET_GAIN_DB,
        note=note,
        markers=tuple(markers),
    )


# ── The presets ─────────────────────────────────────────────────────────────
# Ordered deliberately: the one most likely to be wanted first, then the rest by
# how commonly they are met. The combo shows this order.

PRESETS: tuple[SdrPreset, ...] = (
    _slot_preset(
        key="meshtastic-us",
        label="Meshtastic · US 902-928 · LONG_FAST",
        region="US",
        modem="LONG_FAST",
        slot=_US_SLOT,
        neighbours=4,
        slot_label=f"Ch {_US_SLOT} (nominal start)",
        note=(
            "250 kHz slots, eight across this window, with the four either side "
            f"marked. 906.875 MHz is slot {_US_SLOT} — the usual starting point for a "
            "US mesh — but the firmware picks the real slot by hashing the primary "
            "channel name, so trust the energy over the marker."
        ),
    ),
    _allocation_preset(
        key="meshtastic-eu-868",
        label="Meshtastic · EU 868 · LONG_FAST",
        region="EU_868",
        modem="LONG_FAST",
        note=(
            "The whole EU 869.400-869.650 allocation is 250 kHz wide, so its single "
            "channel fills one marker in the middle of this window. Anything landing "
            "inside that marker is the mesh; anything outside it is something else."
        ),
    ),
    _allocation_preset(
        key="meshtastic-eu-433",
        label="Meshtastic · EU 433 · LONG_FAST",
        region="EU_433",
        modem="LONG_FAST",
        note=(
            "The EU 433 allocation is 1 MHz wide and holds four 250 kHz slots, all "
            "four marked and the window centred on them. This span covers the whole "
            "allocation, so nothing here is off the edge of the picture."
        ),
    ),
    _single_channel_preset(
        key="meshcore-us",
        label="MeshCore · US / ANZ 915",
        technology="MeshCore",
        region="US",
        note=(
            "MeshCore parks on one frequency per region — 910.525 MHz for US/ANZ — "
            "rather than a slot plan, so there is a single marker and the whole "
            "network is inside it. 250 kHz wide at SF11."
        ),
    ),
    _single_channel_preset(
        key="reticulum-us",
        label="Reticulum · US 915 ISM",
        technology="Reticulum",
        region="US",
        note=(
            "Reticulum has no band plan: an RNode is configured with one frequency, "
            "and 915.000 MHz at 125 kHz / SF8 is where an unmodified US RNode is "
            "usually found. If the network set its own frequency, move the centre to "
            "match — the marker is a starting point, not a schedule."
        ),
    ),
    _single_channel_preset(
        key="reticulum-eu",
        label="Reticulum · EU 868 ISM",
        technology="Reticulum",
        region="EU",
        note=(
            "EU RNode networks commonly sit at 867.200 MHz at 125 kHz / SF8. As with "
            "the US preset: the real frequency is in the network's own Reticulum "
            "config, so treat the marker as a starting point."
        ),
    ),
    SdrPreset(
        key="lora-generic-915",
        label="LoRa · unknown modulator, 915 ISM",
        technology="LoRa",
        center_mhz=915.0,
        span_mhz=_PRESET_SPAN_MHZ,
        gain_db=_PRESET_GAIN_DB,
        note=(
            "No markers, because nothing here knows which network this is: for an "
            "unknown LoRa device, tune the centre to its channel. 125 kHz and 250 kHz "
            "channels both resolve inside this window, so the shape of a burst is "
            "readable before you know whose it is."
        ),
    ),
)

PRESETS_BY_KEY: dict[str, SdrPreset] = {preset.key: preset for preset in PRESETS}


def get_preset(key: str) -> SdrPreset | None:
    return PRESETS_BY_KEY.get(key)


def default_preset() -> SdrPreset:
    """The set-up the SIGINT tab opens on, and the same numbers it defaults to."""
    return PRESETS[0]


def technologies() -> tuple[str, ...]:
    """Distinct technologies, in the order the presets introduce them."""
    ordered: list[str] = []
    for preset in PRESETS:
        if preset.technology not in ordered:
            ordered.append(preset.technology)
    return tuple(ordered)
