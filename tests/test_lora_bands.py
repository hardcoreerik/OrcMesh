"""Tests for LoRa band-plan math (Meshtastic slot plan + MeshCore fixed plan)."""
from __future__ import annotations

import pytest

from meshchat.analytics.lora_bands import (
    MESHCORE_PLANS,
    MESHTASTIC_PRESETS,
    MESHTASTIC_REGIONS,
    BandRange,
    meshcore_markers,
    meshtastic_channel_frequency,
    meshtastic_markers,
    meshtastic_num_channels,
)


class TestBandRange:
    def test_center_and_span(self):
        b = BandRange("test", 902.0, 928.0)
        assert b.center_mhz == pytest.approx(915.0)
        assert b.span_mhz == pytest.approx(26.0)


class TestNumChannels:
    def test_us_250khz_has_104_slots(self):
        # 26 MHz / 0.25 MHz = 104
        assert meshtastic_num_channels("US", 250.0) == 104

    def test_us_125khz_has_208_slots(self):
        assert meshtastic_num_channels("US", 125.0) == 208

    def test_unknown_region_is_zero(self):
        assert meshtastic_num_channels("NOPE", 250.0) == 0

    def test_zero_bandwidth_is_zero(self):
        assert meshtastic_num_channels("US", 0.0) == 0

    def test_never_returns_zero_for_valid_region(self):
        # Even an absurdly wide channel yields at least one slot
        assert meshtastic_num_channels("EU_868", 100_000.0) >= 1


class TestChannelFrequency:
    def test_us_channel_zero_is_offset_by_half_bandwidth(self):
        # 902.0 + 0.125 = 902.125
        assert meshtastic_channel_frequency("US", 250.0, 0) == pytest.approx(902.125)

    def test_us_channel_one_steps_one_bandwidth(self):
        assert meshtastic_channel_frequency("US", 250.0, 1) == pytest.approx(902.375)

    def test_channel_wraps_within_region(self):
        count = meshtastic_num_channels("US", 250.0)
        first = meshtastic_channel_frequency("US", 250.0, 0)
        wrapped = meshtastic_channel_frequency("US", 250.0, count)
        assert wrapped == pytest.approx(first)

    def test_all_slots_stay_inside_the_allocation(self):
        band = MESHTASTIC_REGIONS["US"]
        for ch in range(meshtastic_num_channels("US", 250.0)):
            freq = meshtastic_channel_frequency("US", 250.0, ch)
            assert band.start_mhz <= freq <= band.end_mhz

    def test_unknown_region_returns_none(self):
        assert meshtastic_channel_frequency("NOPE", 250.0, 0) is None


class TestMeshtasticMarkers:
    def test_single_marker_by_default(self):
        markers = meshtastic_markers("US", "LONG_FAST", 20)
        assert len(markers) == 1
        assert "active" in markers[0].label

    def test_neighbours_are_included_symmetrically(self):
        markers = meshtastic_markers("US", "LONG_FAST", 20, include_neighbours=2)
        assert len(markers) == 5
        assert sum("active" in m.label for m in markers) == 1

    def test_channel_zero_is_labelled_nominal_not_active(self):
        # 0 means "firmware auto-selects from the channel name hash", which we
        # deliberately do not reimplement — the marker must not overclaim.
        marker = meshtastic_markers("US", "LONG_FAST", 0)[0]
        assert "nominal" in marker.label.lower()
        assert "active" not in marker.label.lower()

    def test_explicit_channel_is_labelled_active(self):
        marker = meshtastic_markers("US", "LONG_FAST", 7)[0]
        assert "active" in marker.label.lower()

    def test_marker_bandwidth_follows_preset(self):
        fast = meshtastic_markers("US", "LONG_FAST", 0)[0]
        slow = meshtastic_markers("US", "LONG_SLOW", 0)[0]
        assert fast.bandwidth_khz == 250.0
        assert slow.bandwidth_khz == 125.0

    def test_unknown_preset_falls_back_to_long_fast_bandwidth(self):
        marker = meshtastic_markers("US", "NOT_A_PRESET", 0)[0]
        assert marker.bandwidth_khz == 250.0

    def test_none_preset_is_handled(self):
        assert meshtastic_markers("US", None, 0)

    def test_unknown_region_yields_nothing(self):
        assert meshtastic_markers("NOPE", "LONG_FAST", 0) == []


class TestMeshCoreMarkers:
    def test_us_plan_marker(self):
        markers = meshcore_markers("US")
        assert len(markers) == 1
        assert markers[0].center_mhz == pytest.approx(910.525)

    def test_unknown_region_yields_nothing(self):
        assert meshcore_markers("NOPE") == []

    def test_every_plan_produces_a_marker(self):
        for key in MESHCORE_PLANS:
            assert len(meshcore_markers(key)) == 1


class TestDataIntegrity:
    def test_every_region_range_is_ordered(self):
        for name, band in MESHTASTIC_REGIONS.items():
            assert band.start_mhz < band.end_mhz, f"{name} range is inverted"

    def test_every_preset_has_sane_values(self):
        for name, (bw, sf) in MESHTASTIC_PRESETS.items():
            assert bw > 0, f"{name} has non-positive bandwidth"
            assert 6 <= sf <= 12, f"{name} has an out-of-range spreading factor"

    def test_meshcore_plans_sit_inside_plausible_ism_bands(self):
        for key, plan in MESHCORE_PLANS.items():
            assert 400.0 < plan.freq_mhz < 1000.0, f"{key} frequency looks wrong"
