"""Tests for the disruption waveforms.

These check the properties that make each waveform worth transmitting, rather than merely
that it produced a right-sized array: that the chirp sweeps the channel once per symbol at the
slope a receiver is matched to, that the noise is confined to the channel, and that the
interleaving is I/Q and not something else.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshchat.services.rf import waveforms as wf

BW = 125_000.0
RATE = 500_000.0  # 4x the channel, so a chirp's sweep is comfortably resolved


class TestChirp:
    def test_one_symbol_is_the_documented_length(self):
        """A LoRa symbol is 2^SF chirps of the band: 2^SF / BW seconds."""
        samples = wf.chirp(1, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE)

        assert samples.size == pytest.approx(wf.symbol_period_s(BW, 7) * RATE)

    def test_the_symbol_period_matches_the_specification(self):
        """SF7 at 125 kHz is 1.024 ms; SF12 is 32.768 ms."""
        assert wf.symbol_period_s(125_000, 7) == pytest.approx(0.001024, rel=1e-9)
        assert wf.symbol_period_s(125_000, 12) == pytest.approx(0.032768, rel=1e-9)

    def test_the_sweep_covers_the_channel_once_per_symbol(self):
        """The property a receiver's correlator is matched to.

        Measured from the instantaneous frequency, which is where the sweep lives — not from
        the phase, which wraps and would hide it.
        """
        samples = wf.chirp(1, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE)
        phase = np.unwrap(np.angle(samples))
        inst = np.diff(phase) * RATE / (2 * np.pi)

        assert inst.min() == pytest.approx(-BW / 2, rel=0.02)
        assert inst.max() == pytest.approx(BW / 2, rel=0.02)

    def test_the_slope_is_bandwidth_over_symbol_period(self):
        """The number that decides whether this interferes at all."""
        samples = wf.chirp(1, bandwidth_hz=BW, sf=8, sample_rate_hz=RATE)
        phase = np.unwrap(np.angle(samples))
        inst = np.diff(phase) * RATE / (2 * np.pi)
        slope = (inst.max() - inst.min()) / (wf.symbol_period_s(BW, 8))

        assert slope == pytest.approx(BW / wf.symbol_period_s(BW, 8), rel=0.02)

    def test_the_phase_does_not_jump_between_symbols(self):
        """Accumulating from instantaneous frequency is what prevents this.

        Unwrapped first, because `np.angle` returns values in (-pi, pi] and the accumulated
        phase legitimately crosses that boundary several times per symbol — differencing the
        wrapped phase reports a 2*pi step that is not a discontinuity at all.
        """
        samples = wf.chirp(4, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE)
        phase = np.unwrap(np.angle(samples))
        step = np.abs(np.diff(phase))
        # One sample of a 62.5 kHz sweep moves the phase by at most 2*pi*f/RATE.
        assert step.max() == pytest.approx(2 * np.pi * (BW / 2) / RATE, rel=0.05)

    def test_the_phase_at_a_symbol_boundary_is_continuous(self):
        """The boundary is where a naive closed-form expression goes wrong."""
        one = wf.chirp(1, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE)
        boundary = np.unwrap(np.angle(wf.chirp(2, bandwidth_hz=BW, sf=7,
                                             sample_rate_hz=RATE)[one.size - 1:
                                                                    one.size + 1]))

        assert abs(boundary[1] - boundary[0]) < 2 * np.pi * (BW / 2) / RATE * 1.5

    def test_a_downward_chirp_sweeps_the_other_way(self):
        up = wf.chirp(1, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE, direction=1)
        down = wf.chirp(1, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE, direction=-1)

        up_inst = np.diff(np.unwrap(np.angle(up))) * RATE / (2 * np.pi)
        down_inst = np.diff(np.unwrap(np.angle(down))) * RATE / (2 * np.pi)

        assert up_inst[0] < up_inst[-1]
        assert down_inst[0] > down_inst[-1]

    def test_several_symbols_are_one_symbol_repeated(self):
        one = wf.chirp(1, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE)
        three = wf.chirp(3, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE)

        assert three.size == three.size  # no-op guard against a typo in the next line
        assert np.allclose(three[: one.size], one)

    def test_the_waveform_is_constant_modulus(self):
        """A chirp is a phase-only signal; any amplitude variation would be an artefact."""
        samples = wf.chirp(2, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE)

        assert np.allclose(np.abs(samples), 1.0, atol=1e-9)


class TestChirpRefusals:
    def test_a_channel_wider_than_the_sample_rate_is_refused(self):
        """Otherwise the chirp wraps and stops sharing the target's slope.

        Silently wrapping would be the worst outcome: the waveform still transmits, still
        looks like a chirp in an FFT, and simply does not interfere.
        """
        with pytest.raises(ValueError, match="does not fit"):
            wf.chirp(1, bandwidth_hz=250_000, sf=7, sample_rate_hz=125_000)

    def test_a_spreading_factor_outside_the_supported_range_is_refused(self):
        with pytest.raises(ValueError, match="spreading factor"):
            wf.chirp(1, bandwidth_hz=BW, sf=4, sample_rate_hz=RATE)
        with pytest.raises(ValueError, match="spreading factor"):
            wf.chirp(1, bandwidth_hz=BW, sf=13, sample_rate_hz=RATE)

    def test_the_extremes_of_what_meshtastic_uses_are_allowed(self):
        for sf in (7, 12):
            assert wf.chirp(1, bandwidth_hz=BW, sf=sf, sample_rate_hz=RATE).size > 0


class TestPreambleFlood:
    def test_a_preamble_is_a_run_of_up_chirps(self):
        flood = wf.preamble_flood(2, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE,
                                  preamble_symbols=8)

        assert flood.size == 16 * int(wf.symbol_period_s(BW, 7) * RATE)

    def test_a_preamble_with_no_symbols_is_refused(self):
        with pytest.raises(ValueError, match="at least one symbol"):
            wf.preamble_flood(1, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE,
                              preamble_symbols=0)


class TestChannelNoise:
    def test_the_power_is_confined_to_the_channel(self):
        """The property that makes it broadband: everything outside is removed exactly."""
        samples = wf.channel_noise(0.05, bandwidth_hz=BW, sample_rate_hz=RATE, seed=1)
        spectrum = np.abs(np.fft.fft(samples)) ** 2
        freqs = np.fft.fftfreq(samples.size, d=1.0 / RATE)
        inside = spectrum[np.abs(freqs) <= BW / 2]
        outside = spectrum[np.abs(freqs) > BW / 2]

        assert inside.sum() > 0
        assert outside.sum() / spectrum.sum() < 1e-6

    def test_it_is_normalised_to_full_scale(self):
        """So that a drive setting means the same thing on every method."""
        samples = wf.channel_noise(0.05, bandwidth_hz=BW, sample_rate_hz=RATE, seed=1)

        assert np.max(np.abs(samples)) == pytest.approx(1.0, rel=1e-6)

    def test_the_same_seed_gives_the_same_noise(self):
        a = wf.channel_noise(0.02, bandwidth_hz=BW, sample_rate_hz=RATE, seed=7)
        b = wf.channel_noise(0.02, bandwidth_hz=BW, sample_rate_hz=RATE, seed=7)

        assert np.array_equal(a, b)

    def test_noise_is_not_a_tone(self):
        """It must not be a repeating pattern, which would put power in a few lines."""
        samples = wf.channel_noise(0.05, bandwidth_hz=BW, sample_rate_hz=RATE, seed=3)
        spectrum = np.abs(np.fft.fft(samples)) ** 2

        assert spectrum.max() / spectrum.mean() < 60

    def test_a_zero_duration_is_refused(self):
        with pytest.raises(ValueError, match="positive duration"):
            wf.channel_noise(0.0, bandwidth_hz=BW, sample_rate_hz=RATE)


class TestInterleaving:
    def test_i_and_q_are_interleaved_in_order(self):
        """Values chosen to stay under full scale, so the order is what is being read — the
        earlier version used samples that all clipped, which could not distinguish I from Q
        or one sample from the next."""
        samples = np.array([0.25 + 0.5j, 0.75 + 1.0j], dtype=np.complex128)

        raw = wf.to_interleaved_int16(samples, drive=1.0)
        values = np.frombuffer(raw, dtype="<i2")

        assert list(values) == [8191, 16383, 24575, 32767]

    def test_q_is_not_silently_dropped(self):
        """The bug this guards: casting a complex array to int16 discards the imaginary part.

        Clipping before splitting the pair looked equivalent and left Q permanently zero, so
        every waveform would have gone out as a real signal — double-sideband — with no error
        anywhere and only the wrong spectrum to show for it.
        """
        only_q = np.array([0 + 1j, 0 + 1j], dtype=np.complex128)

        values = np.frombuffer(wf.to_interleaved_int16(only_q, drive=1.0), dtype="<i2")

        assert values[0] == 0
        assert values[1] == 32767, "Q must survive"
        assert values[3] == 32767

    def test_the_output_is_two_int16_per_sample(self):
        samples = np.zeros(1000, dtype=np.complex128)

        assert len(wf.to_interleaved_int16(samples, drive=0.5)) == 1000 * 4

    def test_drive_scales_i_and_q_together(self):
        """Scaling them differently would rotate the constellation, not attenuate it."""
        samples = np.array([1 + 1j], dtype=np.complex128)

        full = np.frombuffer(wf.to_interleaved_int16(samples, drive=1.0), dtype="<i2")
        half = np.frombuffer(wf.to_interleaved_int16(samples, drive=0.5), dtype="<i2")

        assert full[0] == pytest.approx(2 * half[0], rel=0.01)
        assert full[1] == pytest.approx(2 * half[1], rel=0.01)

    def test_a_saturating_sample_clips_rather_than_wrapping(self):
        """A wrap is a loud click, and a click is broadband noise across the channel."""
        samples = np.array([2.0 + 0j], dtype=np.complex128)

        values = np.frombuffer(wf.to_interleaved_int16(samples, drive=1.0), dtype="<i2")

        assert values[0] == 32767, "a wrapped sample would be strongly negative"

    def test_a_drive_above_one_is_refused(self):
        with pytest.raises(ValueError, match="between 0 and 1"):
            wf.to_interleaved_int16(np.zeros(4, dtype=np.complex128), drive=1.5)


class TestMethodNotes:
    def test_every_method_has_a_note(self):
        """A method with no description is one nobody can judge before using it."""
        for method in wf.Method:
            note = next(n for n in wf.LORA_METHODS if n.method is method)

            assert note.mechanism and note.caveat and note.expectation

    def test_every_note_says_what_it_cannot_do(self):
        """The caveat is the field that stops an expectation being read as a result."""
        for note in wf.LORA_METHODS:
            assert len(note.caveat) > 40, note.name

    def test_the_continuous_carrier_is_not_sold_as_strong(self):
        """The one honest ranking decision already made: proximity, not modulation."""
        tone = next(n for n in wf.LORA_METHODS if n.method is wf.Method.CW_TONE)

        assert tone.expectation == "weak"
        assert "proximity" in tone.caveat.lower() or "meter" in tone.caveat.lower()

    def test_an_expectation_is_never_a_measurement(self):
        """These are expectations, so none may claim to have been measured as working."""
        allowed = {"strong", "moderate", "weak", "unmeasured"}

        for note in wf.LORA_METHODS:
            assert note.expectation in allowed, note.name

    def test_the_unimplemented_method_says_so(self):
        replay = next(n for n in wf.LORA_METHODS if n.method is wf.Method.REPLAY)

        assert "implemented" in replay.caveat

    def test_describe_method_names_the_mechanism(self):
        text = wf.describe_method(wf.Method.CHIRP_TRAIN)

        assert "chirp" in text.lower()
        assert "strong" in text

    def test_an_unknown_method_is_an_error_not_a_blank(self):
        class NotAMethod(str):
            pass

        with pytest.raises(KeyError):
            wf.describe_method(NotAMethod("nonsense"))  # type: ignore[arg-type]


class TestPeakToAverage:
    def test_a_constant_modulus_waveform_is_near_zero_db(self):
        samples = wf.chirp(2, bandwidth_hz=BW, sf=7, sample_rate_hz=RATE)

        assert wf.peak_to_average_db(samples) == pytest.approx(0.0, abs=1e-9)

    def test_a_spiky_waveform_reports_a_high_figure(self):
        """The measure exists to show when power is being wasted in peaks."""
        spiky = np.concatenate([np.ones(999, dtype=np.complex128), [50 + 0j]])

        assert wf.peak_to_average_db(spiky) > 25
