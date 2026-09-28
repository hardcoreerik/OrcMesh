"""Waveforms for disrupting LoRa, and what each one is actually good for.

Researched before being written. The open-source ecosystem splits in two: a handful of
jammer projects (mostly dissertation-grade GNU Radio or HackRF builds — `joejenkins4/LoRa-Jammer`,
`retronav/lorapwn-jammer`) and a larger body of *detection* work
(`Albertokeroro/edge-ai-lora-anomaly-detection`, `netlab-sapienza/LoRaWAN_jammer_ns3`).
The reference for the physical layer is `tapparelj/gr-lora_sdr` (EPFL, GPL-3.0): a complete
LoRa transceiver including chirp modulation and sync-word handling.

The chirp maths below is reimplemented from the modulation definition rather than copied, and
one thing here is deliberately *not* what those projects do. They transmit and stop. This one
is built to be measured: every waveform is pure and testable, and the point of the rig is to
compare a method's effect against a real receiver rather than to assume it.

**Why a CW tone is the weakest of these, stated plainly.** LoRa is chirp spread spectrum. A
narrowband carrier occupies one sliver of a 125 kHz channel, and the dechirping correlator
integrates across the whole symbol, so the interferer is spread away by the processing gain.
A CW tone only works at all because a nearby transmitter can overload the receiver front end —
which is a *proximity* effect, not a modulation one. See `LORA_METHODS` for the ranking.

**The one that is genuinely efficient** is a chirp train at the same spreading factor: the
receiver's correlator is matched to that slope, so a jammer chirp is *correlated* rather than
rejected, and its energy lands in the demodulator instead of being spread across the band.
Same spreading factor and bandwidth are therefore the two parameters that matter most, far
more than getting the payload or sync word right — for disruption, symbol encoding is almost
irrelevant; the slope is what the receiver responds to.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import numpy as np

log = logging.getLogger(__name__)

#: Shortest and longest spreading factors worth offering. 7-12 is what Meshtastic uses;
#: 5 and 6 exist in the LoRa specification but are not supported by SX126x radios, which is
#: the chip in the T-Beam on this bench.
SF_MIN, SF_MAX = 5, 12

#: Channel bandwidths LoRa defines. 125 kHz is Meshtastic's default.
BANDWIDTHS_HZ = (125_000, 250_000, 500_000)


class Method(Enum):
    """The methods worth having, ordered by nothing in particular — see `LORA_METHODS`."""

    CW_TONE = "cw_tone"
    CHANNEL_NOISE = "channel_noise"
    CHIRP_TRAIN = "chirp_train"
    PREAMBLE_FLOOD = "preamble_flood"
    REPLAY = "replay"


@dataclass(frozen=True)
class MethodNote:
    """One method, and how well it can be expected to work. The honest part of the feature."""

    method: Method
    name: str
    mechanism: str
    #: "strong" / "moderate" / "weak" / "unmeasured" — an expectation, never a result. The
    #: only thing that turns one of these into a finding is a measurement against a receiver,
    #: which is what the panel exists to make easy.
    expectation: str
    caveat: str


#: What each method does, and what it costs. Ordered best-first, because a list of five
#: options with no ranking invites the worst one to be tried first and believed.
LORA_METHODS: tuple[MethodNote, ...] = (
    MethodNote(
        method=Method.CHIRP_TRAIN,
        name="Same-slope chirp train",
        mechanism=(
            "Repeats chirps at the target's own spreading factor and bandwidth. The receiver's "
            "correlator is matched to that slope, so these are correlated rather than spread "
            "away, and the energy arrives in the demodulator."
        ),
        expectation="strong",
        caveat=(
            "Needs the target's spreading factor, which Meshtastic does not broadcast in the "
            "clear — a mismatch costs most of the advantage. Sweep the spreading factors if it "
            "is not known."
        ),
    ),
    MethodNote(
        method=Method.PREAMBLE_FLOOD,
        name="Preamble flood",
        mechanism=(
            "A train of preamble chirps with no payload. Receivers lock onto preambles and then "
            "wait for a header that never arrives, so the demodulator is held in the wrong state "
            "without needing any packet content."
        ),
        expectation="strong",
        caveat=(
            "Very close to CHIRP_TRAIN in effect; the difference is that this one holds "
            "receivers in acquisition rather than merely colliding with them. Both need the "
            "right spreading factor."
        ),
    ),
    MethodNote(
        method=Method.CHANNEL_NOISE,
        name="Channel-band noise",
        mechanism=(
            "Band-limited noise filling the channel. Broadband, so there is no narrow feature "
            "for the correlator to reject; it raises the floor the signal has to be found in."
        ),
        expectation="moderate",
        caveat=(
            "Spends power across the whole channel rather than matching the receiver, so it "
            "needs more of it than a chirp train for the same effect on a spread signal. "
            "The reliable choice when the spreading factor is unknown."
        ),
    ),
    MethodNote(
        method=Method.REPLAY,
        name="Captured-packet replay",
        mechanism=(
            "Retransmits packets already captured from the mesh. Meshtastic suppresses "
            "duplicates, so genuine traffic arriving after a replay can be discarded as "
            "already seen."
        ),
        expectation="unmeasured",
        caveat=(
            "Uses no jamming power at all, which makes it the cheapest method here — and the "
            "only one that needs captured packets rather than a waveform. Not implemented; it "
            "needs the capture and format work from the recorder first."
        ),
    ),
    MethodNote(
        method=Method.CW_TONE,
        name="Continuous carrier",
        mechanism=(
            "A single steady tone. Only disruptive by overloading a nearby receiver's front "
            "end; against LoRa's processing gain it is the narrowest possible interferer."
        ),
        expectation="weak",
        caveat=(
            "Works at bench range because of proximity (a metre puts roughly 90 dB of margin "
            "over LoRa's sensitivity floor) and will stop working as soon as the radios are "
            "further apart. Do not read a success here as a spectral result."
        ),
    ),
)


def symbol_period_s(bandwidth_hz: float, sf: int) -> float:
    """How long one LoRa symbol lasts: 2^SF chirps of the band, at SF/BW seconds each."""
    return 2**sf / bandwidth_hz


def _check_rates(bandwidth_hz: float, sample_rate_hz: float) -> None:
    if bandwidth_hz <= 0:
        raise ValueError("bandwidth must be positive")
    if sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    if sample_rate_hz < bandwidth_hz:
        raise ValueError(
            f"a {bandwidth_hz / 1e3:g} kHz channel does not fit in a "
            f"{sample_rate_hz / 1e3:g} kHz sample rate — the chirp would wrap and no longer "
            "share the target's slope",
        )


def _check(sf: int, bandwidth_hz: float, sample_rate_hz: float) -> None:
    _check_rates(bandwidth_hz, sample_rate_hz)
    if not SF_MIN <= sf <= SF_MAX:
        raise ValueError(f"spreading factor must be {SF_MIN}..{SF_MAX}, got {sf}")


def chirp(symbols: int, *, bandwidth_hz: float, sf: int, sample_rate_hz: float,
          direction: int = 1) -> np.ndarray:
    """`symbols` LoRa chirps of symbol value zero, as complex baseband samples.

    The instantaneous frequency sweeps the whole channel once per symbol — that sweep rate,
    `bandwidth / symbol_period`, is the slope a receiver's correlator is matched to, which is
    the entire reason this waveform is worth more than noise against a LoRa target.

    Phase is accumulated from the instantaneous frequency rather than integrated in closed
    form, so the wrap at the top of the sweep is continuous. A closed-form expression without
    that care produces a phase jump every symbol, which smears the spectrum and quietly
    undoes the slope matching that makes this work.

    `direction` of -1 sweeps downward, which is what a LoRa sync word uses for some of its
    symbols. For disruption it rarely matters; it is here because a receiver hunting a
    downward chirp is a different target from one hunting an upward one.
    """
    _check(sf, bandwidth_hz, sample_rate_hz)
    period = symbol_period_s(bandwidth_hz, sf)
    per_symbol = int(round(period * sample_rate_hz))
    if per_symbol < 1:
        raise ValueError("sample rate is too low for one symbol")
    t = np.arange(per_symbol, dtype=np.float64) / sample_rate_hz
    # One full sweep across the channel per symbol, starting at the bottom for an up-chirp.
    fraction = (t / period) % 1.0
    frequency = direction * (fraction - 0.5) * bandwidth_hz
    phase = 2 * np.pi * np.cumsum(frequency) / sample_rate_hz
    return np.tile(np.exp(1j * phase), symbols)


def preamble_flood(preambles: int, *, bandwidth_hz: float, sf: int, sample_rate_hz: float,
                   preamble_symbols: int = 8) -> np.ndarray:
    """`preambles` back-to-back LoRa preambles, with no header or payload after them.

    A real LoRa preamble is a run of up-chirps; a receiver detects it, locks its timing, then
    looks for a header. Sending preambles and nothing else therefore occupies the acquisition
    state directly instead of relying on collision, which is why this is expected to be strong
    despite carrying no information at all.
    """
    if preamble_symbols < 1:
        raise ValueError("a preamble needs at least one symbol")
    return chirp(
        preambles * preamble_symbols, bandwidth_hz=bandwidth_hz, sf=sf,
        sample_rate_hz=sample_rate_hz,
    )


def channel_noise(seconds: float, *, bandwidth_hz: float, sample_rate_hz: float,
                  seed: int | None = None) -> np.ndarray:
    """Band-limited complex noise filling a channel of `bandwidth_hz`.

    Shaped in the frequency domain rather than by a filter, so the edges are where they were
    asked to be rather than wherever a filter's roll-off put them. Everything outside the
    channel is removed exactly, which matters because the point of this waveform is that it
    occupies the channel and nothing else.

    The amplitude is normalised to the same full-scale drive the tone uses, so a "75%" setting
    means the same thing on every method — comparing methods whose levels were set
    differently would compare nothing.
    """
    _check_rates(bandwidth_hz, sample_rate_hz)
    if seconds <= 0:
        raise ValueError("need a positive duration")
    count = int(round(seconds * sample_rate_hz))
    rng = np.random.default_rng(seed)
    spectrum = rng.normal(size=count) + 1j * rng.normal(size=count)
    freqs = np.fft.fftfreq(count, d=1.0 / sample_rate_hz)
    spectrum = np.where(np.abs(freqs) <= bandwidth_hz / 2, spectrum, 0)
    shaped = np.fft.ifft(spectrum)
    peak = np.max(np.abs(shaped))
    return shaped / peak if peak > 0 else shaped


def to_interleaved_int16(samples: np.ndarray, *, drive: float) -> bytes:
    """Complex samples as interleaved little-endian I/Q for the transmitter's DMA.

    `drive` scales the pair together, so I and Q stay in the same relationship — scaling them
    differently would rotate the constellation rather than attenuate it, which for a
    quadrature pair is a carrier plus a distorted image of itself.

    Clipped rather than allowed to wrap: a sample that saturates is a small distortion, while
    one that wraps is a loud click, and a click is broadband noise across the whole channel —
    the opposite of what a carefully band-limited waveform is for.

    I and Q are separated **before** the clip and cast. Clipping the complex array first and
    then reading `.imag` looks equivalent and is not: `astype(np.int16)` on a complex array
    discards the imaginary part, so every waveform would go out with a permanently zero Q — a
    real signal rather than a complex one, and therefore double-sideband. It produced no error
    and no warning in the transmit path, only the wrong spectrum.
    """
    if not 0.0 <= drive <= 1.0:
        raise ValueError("drive must be between 0 and 1")
    scale = drive * 32767.0
    in_phase = np.clip(samples.real * scale, -32767.0, 32767.0).astype(np.int16)
    quadrature = np.clip(samples.imag * scale, -32767.0, 32767.0).astype(np.int16)
    interleaved = np.empty(in_phase.size * 2, dtype=np.int16)
    interleaved[0::2] = in_phase
    interleaved[1::2] = quadrature
    return interleaved.astype("<i2").tobytes()


def peak_to_average_db(samples: np.ndarray) -> float:
    """How far the waveform's peak sits above its average. Higher is less efficient.

    Reported because it is the difference between a jammer that spends its power evenly and one
    that spends it in spikes: a high figure means most of the power is wasted in the peaks and
    the average is what the receiver actually sees.
    """
    power = np.abs(samples) ** 2
    mean = float(np.mean(power))
    if mean <= 0:
        return float("inf")
    return float(10 * np.log10(np.max(power) / mean))


def describe_method(method: Method) -> str:
    """One line about a method, for a tooltip that should not lie or oversell."""
    for note in LORA_METHODS:
        if note.method is method:
            return f"{note.name} ({note.expectation}): {note.mechanism}"
    raise KeyError(method)
