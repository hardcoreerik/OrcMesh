"""Tests for LoRa time-on-air.

The expected milliseconds here are worked through the formula by hand rather than
copied from the implementation, so the arithmetic is actually being checked. The
SF11/125 kHz/16-byte case is also the widely published ~660 ms figure, which is
the one independent anchor available.
"""
from __future__ import annotations

import pytest

from meshchat.analytics.lora_airtime import (
    ModemParams,
    airtime_ms,
    airtime_seconds,
    duty_cycle,
    params_for_preset,
    payload_symbols,
)
from meshchat.analytics.lora_bands import MESHTASTIC_PRESETS

#: 4/5 coding, explicit header, CRC on, 8-symbol preamble — Meshtastic's defaults.
def _params(spreading_factor: int, bandwidth_khz: float) -> ModemParams:
    return ModemParams(spreading_factor=spreading_factor, bandwidth_hz=bandwidth_khz * 1000.0)


class TestSymbolTime:
    def test_it_is_the_spreading_factor_over_the_bandwidth(self):
        # 2^7 / 125000 = 1.024 ms
        assert _params(7, 125.0).symbol_time_ms == pytest.approx(1.024)

    def test_a_higher_spreading_factor_takes_longer(self):
        # 2^12 / 125000 = 32.768 ms
        assert _params(12, 125.0).symbol_time_ms == pytest.approx(32.768)

    def test_more_bandwidth_is_faster(self):
        # 2^7 / 500000 = 0.256 ms
        assert _params(7, 500.0).symbol_time_ms == pytest.approx(0.256)

    def test_the_default_preset_symbol_time(self):
        # LONG_FAST is SF11 at 250 kHz: 2^11 / 250000 = 8.192 ms
        assert params_for_preset("LONG_FAST").symbol_time_ms == pytest.approx(8.192)


class TestLowDataRateOptimisation:
    """The radio enables this itself once a symbol outlasts 16 ms."""

    @pytest.mark.parametrize("spreading_factor,bandwidth_khz,expected", [
        (7, 125.0, False),    # 1.024 ms
        (10, 125.0, False),   # 8.192 ms
        (11, 125.0, True),    # 16.384 ms — just over
        (11, 250.0, False),   # 8.192 ms — the same SF, faster
        (12, 125.0, True),    # 32.768 ms
        (12, 250.0, True),    # 16.384 ms — just over
        (12, 62.5, True),     # 65.536 ms
    ])
    def test_the_16ms_rule(self, spreading_factor, bandwidth_khz, expected):
        params = _params(spreading_factor, bandwidth_khz)

        assert params.low_data_rate_optimise is expected

    def test_it_costs_symbols_when_enabled(self):
        """DE shrinks the payload denominator, so more symbols carry the payload.

        There is no way to express "SF11 at 125 kHz with DE *off*" through
        ModemParams, because DE is derived from the geometry rather than set —
        so the contrast is drawn against 250 kHz, where the same spreading
        factor keeps a short enough symbol to leave DE off.
        """
        assert _params(11, 125.0).low_data_rate_optimise
        assert _params(11, 250.0).low_data_rate_optimise is False

        assert payload_symbols(16, _params(11, 125.0)) == 28, "DE on"
        assert payload_symbols(16, _params(11, 250.0)) == 23, "DE off"


class TestPayloadSymbols:
    def test_worked_through_by_hand(self):
        # SF7/125 kHz, 16 bytes, CRC on, explicit header, DE off:
        #   numerator   = 128 - 28 + 28 + 16 = 144
        #   denominator = 4 * 7 = 28
        #   144/28 = 5.14 -> 6 blocks, 6 * 5 = 30, plus the 8 mandatory = 38
        assert payload_symbols(16, _params(7, 125.0)) == 38

    def test_the_same_payload_under_de(self):
        # SF11/125 kHz has DE on:
        #   numerator   = 128 - 44 + 28 + 16 = 128
        #   denominator = 4 * (11 - 2) = 36
        #   128/36 = 3.56 -> 4 blocks, 4 * 5 = 20, plus 8 = 28
        assert payload_symbols(16, _params(11, 125.0)) == 28

    def test_at_the_default_preset(self):
        # SF11/250 kHz, DE off: 128 / 44 = 2.91 -> 3 blocks, 8 + 15 = 23
        assert payload_symbols(16, _params(11, 250.0)) == 23

    def test_there_is_always_a_minimum_of_eight(self):
        """A tiny payload must not fall below the mandatory 8 symbols."""
        assert payload_symbols(0, _params(7, 125.0)) == 13
        assert payload_symbols(0, _params(12, 125.0)) >= 8

    def test_more_payload_means_more_symbols(self):
        counts = [payload_symbols(size, _params(11, 250.0)) for size in (1, 16, 64, 200)]

        assert counts == sorted(counts)
        assert counts[0] < counts[-1]

    def test_an_implicit_header_needs_no_more_symbols(self):
        explicit = payload_symbols(16, _params(11, 250.0))
        implicit = payload_symbols(
            16, ModemParams(spreading_factor=11, bandwidth_hz=250_000.0, explicit_header=False)
        )

        assert implicit <= explicit

    def test_dropping_the_crc_never_costs_more(self):
        with_crc = payload_symbols(16, _params(11, 250.0))
        without = payload_symbols(
            16, ModemParams(spreading_factor=11, bandwidth_hz=250_000.0, crc=False)
        )

        assert without <= with_crc

    def test_a_weaker_coding_rate_costs_more_symbols(self):
        rate_45 = payload_symbols(16, _params(11, 250.0))
        rate_46 = payload_symbols(
            16, ModemParams(spreading_factor=11, bandwidth_hz=250_000.0, coding_rate=2)
        )

        assert rate_46 > rate_45


class TestAirtime:
    def test_the_well_known_sf11_figure(self):
        """SF11 at 125 kHz with 16 bytes is the ~660 ms case people cite.

        Symbols = 28 payload + 8 preamble + 4.25 = 40.25, at 16.384 ms each.
        """
        assert airtime_ms(16, _params(11, 125.0)) == pytest.approx(659.5, abs=1.0)

    def test_the_fast_end(self):
        # 38 + 8 + 4.25 = 50.25 symbols at 1.024 ms
        assert airtime_ms(16, _params(7, 125.0)) == pytest.approx(51.5, abs=0.5)

    def test_the_slowest_preset(self):
        # 28 + 8 + 4.25 = 40.25 symbols at 32.768 ms
        assert airtime_ms(16, _params(12, 125.0)) == pytest.approx(1318.9, abs=1.0)

    def test_the_default_preset(self):
        # SF11/250 kHz, DE off: 23 + 8 + 4.25 = 35.25 symbols at 8.192 ms
        assert airtime_ms(16, _params(11, 250.0)) == pytest.approx(288.8, abs=1.0)

    def test_doubling_the_bandwidth_halves_the_time(self):
        """Exactly true only while DE does not change with the bandwidth.

        At SF7 a symbol is far shorter than 16 ms at either bandwidth, so the
        symbol count is identical and the time is exactly halved. At SF11 it is
        not — see the test below — which is why this is checked here rather than
        assumed everywhere.
        """
        wide = airtime_ms(16, _params(7, 250.0))
        narrow = airtime_ms(16, _params(7, 125.0))

        assert wide == pytest.approx(narrow / 2)

    def test_at_sf11_halving_the_bandwidth_costs_more_than_double(self):
        """Because DE also switches on, the payload needs extra symbols too."""
        slow = airtime_ms(16, _params(11, 125.0))
        fast = airtime_ms(16, _params(11, 250.0))

        assert slow > fast * 2

    def test_doubling_the_spreading_factor_roughly_doubles_the_time(self):
        assert airtime_ms(16, _params(12, 125.0)) / airtime_ms(16, _params(11, 125.0)) == (
            pytest.approx(2.0, abs=0.05)
        )

    def test_the_preamble_is_counted_exactly(self):
        """12.25 symbols of preamble must show up as 12.25 symbols of time."""
        params = _params(11, 250.0)
        longer = ModemParams(
            spreading_factor=11, bandwidth_hz=250_000.0, preamble_symbols=20
        )

        difference = airtime_ms(16, longer) - airtime_ms(16, params)

        assert difference == pytest.approx(12 * params.symbol_time_ms)

    def test_the_payload_size_monotonically_increases_the_time(self):
        times = [airtime_ms(size, _params(11, 250.0)) for size in (1, 8, 16, 64, 128, 200)]

        assert times == sorted(times)

    def test_seconds_agree_with_milliseconds(self):
        params = _params(11, 250.0)

        assert airtime_seconds(16, params) == pytest.approx(airtime_ms(16, params) / 1000.0)

    def test_a_long_fast_packet_moves_about_3_per_second_when_alone(self):
        """A concrete sense of scale for the UI: 16 bytes costs ~289 ms."""
        assert 1 / airtime_seconds(16, _params(11, 250.0)) == pytest.approx(3.5, abs=0.2)


class TestPresetTable:
    @pytest.mark.parametrize("preset", sorted(MESHTASTIC_PRESETS))
    def test_every_preset_gets_slower_as_the_table_orders_them(self, preset):
        """Validates the band plan's table through the formula.

        The table is ordered fastest to slowest, so a preset's 16-byte airtime
        must increase down it. If a spreading factor or bandwidth were ever
        mistyped, this is where it shows.
        """
        order = list(MESHTASTIC_PRESETS)
        times = [airtime_ms(16, params_for_preset(name)) for name in order]

        assert preset in order
        assert times == sorted(times), f"{order} must be fastest first"

    def test_it_reads_the_bandwidth_and_spreading_factor_from_the_band_plan(self):
        params = params_for_preset("LONG_SLOW")

        assert params.spreading_factor == 12
        assert params.bandwidth_hz == 125_000.0

    def test_an_unknown_preset_falls_back_to_the_default(self):
        """Packets arrive from radios whose preset we were never told."""
        fallback = params_for_preset("NOT_A_PRESET")

        assert fallback == params_for_preset("LONG_FAST")

    def test_no_preset_at_all_falls_back_to_the_default(self):
        assert params_for_preset(None) == params_for_preset("LONG_FAST")

    def test_meshtastic_uses_four_fifths_coding(self):
        """Every preset shares this; if that changes, airtime changes with it."""
        assert params_for_preset("LONG_FAST").coding_rate == 1


class TestDutyCycle:
    def test_a_share_of_the_window(self):
        assert duty_cycle(0.5, 10.0) == pytest.approx(0.05)

    def test_a_channel_can_be_fully_occupied(self):
        assert duty_cycle(5.0, 5.0) == pytest.approx(1.0)

    def test_more_traffic_than_the_window_allows_is_reported_not_hidden(self):
        """Above 1 means the caller mixed units or overlapped ranges."""
        assert duty_cycle(6.0, 5.0) > 1.0

    def test_a_zero_window_is_an_error(self):
        with pytest.raises(ValueError):
            duty_cycle(1.0, 0.0)
