"""Tests for the capture presets.

The point of these is not that the numbers look tidy — it is that a preset can never
drift from the band plans. Every frequency is re-derived here from `lora_bands` and
compared, so if a slot plan changes, the preset that claims to sit on it fails.
"""
from __future__ import annotations

import pytest

from meshchat.analytics.lora_bands import (
    MESHTASTIC_PRESETS,
    MESHTASTIC_REGIONS,
    MESHCORE_PLANS,
    RETICULUM_PLANS,
    meshtastic_channel_frequency,
)
from meshchat.analytics.sdr_presets import (
    PRESETS,
    PRESETS_BY_KEY,
    default_preset,
    get_preset,
    technologies,
)

#: What the app's SDR controls allow. A preset outside these would be clamped
#: silently by the spin boxes and the operator would never see what was asked for.
_TUNABLE_MHZ = (24.0, 1766.0)
_RATE_MSPS = (0.25, 3.2)
_GAIN_DB = (0.0, 49.6)


class TestEveryPresetIsUsable:
    def test_keys_are_unique(self):
        keys = [preset.key for preset in PRESETS]

        assert len(keys) == len(set(keys))

    def test_it_is_tunable(self):
        for preset in PRESETS:
            assert _TUNABLE_MHZ[0] <= preset.center_mhz <= _TUNABLE_MHZ[1], preset.key

    def test_the_rate_and_gain_are_within_the_controls(self):
        for preset in PRESETS:
            assert _RATE_MSPS[0] <= preset.span_mhz <= _RATE_MSPS[1], preset.key
            assert _GAIN_DB[0] <= preset.gain_db <= _GAIN_DB[1], preset.key

    def test_it_says_what_it_is_for(self):
        for preset in PRESETS:
            assert preset.label.strip(), preset.key
            assert preset.technology.strip(), preset.key
            # The note is the whole reason a preset can be trusted over the four
            # controls it replaces, so it has to be a real sentence.
            assert len(preset.note) > 40, preset.key

    def test_its_centre_is_inside_its_span_of_its_own_markers(self):
        """A marker off the end of the window would be invisible when applied."""
        for preset in PRESETS:
            half = preset.span_mhz / 2
            for marker in preset.markers:
                assert abs(marker.center_mhz - preset.center_mhz) <= half, (
                    f"{preset.key}: {marker.label} falls outside the window"
                )

    def test_it_describes_itself(self):
        text = default_preset().describe()

        assert f"{default_preset().center_mhz:.4f}" in text
        assert "MHz" in text


class TestMeshtasticPresets:
    def test_each_one_is_centred_on_the_channels_it_marks(self):
        """A channel frequency, or the middle of the block of channels it marks.

        The US preset centres on one slot. The EU ones cover an allocation too narrow
        to fill the window, so they centre on the whole group — which lands between
        two slots by construction, and that is the point of them.
        """
        for preset in PRESETS:
            if preset.technology != "Meshtastic":
                continue
            assert preset.markers, preset.key
            low = min(m.center_mhz for m in preset.markers)
            high = max(m.center_mhz for m in preset.markers)

            assert low <= preset.center_mhz <= high, preset.key
            assert preset.center_mhz == pytest.approx((low + high) / 2), preset.key

    def test_the_us_preset_centres_on_slot_nineteen(self):
        preset = get_preset("meshtastic-us")
        bandwidth_khz, _sf = MESHTASTIC_PRESETS["LONG_FAST"]

        assert preset is not None
        assert preset.center_mhz == pytest.approx(
            meshtastic_channel_frequency("US", bandwidth_khz, 19)
        )

    def test_every_marker_is_inside_the_allocation(self):
        for preset in PRESETS:
            if preset.region is None:
                continue
            band = MESHTASTIC_REGIONS[preset.region]
            for marker in preset.markers:
                assert band.start_mhz <= marker.center_mhz <= band.end_mhz, (
                    f"{preset.key}: {marker.label} is outside {band.name}"
                )

    def test_the_region_is_one_the_region_menu_knows(self):
        for preset in PRESETS:
            if preset.technology == "Meshtastic":
                assert preset.region in MESHTASTIC_REGIONS

    def test_a_wide_region_marks_its_neighbours(self):
        """Adjacent meshes on the same band are the thing worth seeing."""
        preset = get_preset("meshtastic-us")

        assert preset is not None
        assert len(preset.markers) > 1, "a 26 MHz allocation needs neighbour markers"


class TestSingleChannelPresets:
    def test_meshcore_marks_the_plan_frequency(self):
        preset = get_preset("meshcore-us")

        assert preset is not None
        assert preset.center_mhz == pytest.approx(MESHCORE_PLANS["US"].freq_mhz)
        assert len(preset.markers) == 1

    def test_reticulum_marks_the_region_allocation(self):
        for key, region in (("reticulum-us", "US"), ("reticulum-eu", "EU")):
            preset = get_preset(key)

            assert preset is not None
            assert preset.center_mhz == pytest.approx(RETICULUM_PLANS[region].freq_mhz)
            assert len(preset.markers) == 1

    def test_an_unknown_modulator_gets_no_markers(self):
        """Marking a channel for a network nobody has identified would be a guess."""
        preset = get_preset("lora-generic-915")

        assert preset is not None
        assert preset.markers == ()
        assert "tune the centre" in preset.note

    def test_no_region_reticulum_plans_still_cover_both_ismo_bands(self):
        """The two presets that ship must be the two most common networks."""
        assert RETICULUM_PLANS["US"].bandwidth_khz == 125.0
        assert RETICULUM_PLANS["EU"].bandwidth_khz == 125.0


class TestHonesty:
    def test_no_preset_claims_a_slot_is_active(self):
        """A preset starts from a nominal slot; only a radio can say which is live.

        The label rules elsewhere in the app read "active" as "this is where the
        mesh is", so a preset that wrote it would be asserting something it read
        out of a table rather than off the air.
        """
        for preset in PRESETS:
            for marker in preset.markers:
                assert "active" not in marker.label.lower(), (
                    f"{preset.key}: {marker.label}"
                )

    def test_the_us_slot_is_labelled_as_a_starting_point(self):
        preset = get_preset("meshtastic-us")

        assert preset is not None
        primary = next(m for m in preset.markers if "nominal" in m.label.lower())
        assert primary.center_mhz == pytest.approx(906.875)
        assert "hash" in preset.note, "the note must say the firmware picks the slot"


class TestRegistry:
    def test_lookup_by_key(self):
        assert get_preset("meshtastic-us") is PRESETS_BY_KEY["meshtastic-us"]
        assert get_preset("no-such-preset") is None

    def test_the_default_is_first_and_is_the_us_mesh(self):
        assert default_preset() is PRESETS[0]
        assert default_preset().key == "meshtastic-us"

    def test_every_technology_the_brief_named_is_covered(self):
        assert set(technologies()) >= {"Meshtastic", "MeshCore", "Reticulum", "LoRa"}

    def test_technology_order_follows_the_preset_order(self):
        assert technologies()[0] == "Meshtastic"
