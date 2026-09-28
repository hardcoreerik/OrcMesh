"""LoRa time-on-air: how much of the channel each transmission actually costs.

A radio reports its *own* air utilisation (the firmware's ``air_util_tx``), but
nothing reports what everyone else is consuming. That is computable, though:
Semtech's time-on-air formula (AN1200.13) needs only the payload size and the
modem parameters, and a received packet gives us both. It is what makes "who is
hogging the channel" answerable, and it shows why a 16-byte packet costs ~29 ms
on the default LONG_FAST preset but ~1.3 s on LONG_SLOW.

In symbols::

    Tsym      = 2^SF / BW
    Tpreamble = (n_preamble + 4.25) * Tsym
    n_payload = 8 + ceil((8*PL - 4*SF + 28 + 16*CRC - 20*IH) / (4*(SF - 2*DE))) * (CR + 4)
    ToA       = Tpreamble + n_payload * Tsym

``CR`` is the coding-rate numerator (1 for 4/5, which is what Meshtastic uses),
``IH`` is 1 for an implicit header, and ``DE`` is the low-data-rate optimisation
that the radio enables by itself once a symbol lasts longer than 16 ms.

Everything here is pure: sample in, milliseconds out. Presets come from
``analytics.lora_bands`` so there is one copy of the band plan.
"""
from __future__ import annotations

from dataclasses import dataclass

from meshchat.analytics.lora_bands import MESHTASTIC_PRESETS

#: The radio turns the low-data-rate optimisation on by itself when a symbol
#: lasts longer than this. Getting it wrong changes the answer by about a
#: quarter, and it is what makes LONG_MODERATE (SF11 at 125 kHz) noticeably
#: slower than LONG_FAST (SF11 at 250 kHz) beyond the halved bandwidth.
_DE_SYMBOL_THRESHOLD_MS = 16.0

#: Meshtastic's default preset, and what an unknown name falls back to. Matches
#: the ``(250.0, 11)`` default in ``lora_bands.meshtastic_markers``.
_DEFAULT_PRESET = "LONG_FAST"


@dataclass(frozen=True)
class ModemParams:
    """The modem settings that decide how long a transmission takes."""

    spreading_factor: int
    bandwidth_hz: float
    #: Coding-rate numerator: 1 means 4/5, 2 means 4/6, and so on.
    coding_rate: int = 1
    preamble_symbols: int = 8
    explicit_header: bool = True
    crc: bool = True

    @property
    def symbol_time_ms(self) -> float:
        """Duration of one LoRa symbol. Halving the bandwidth doubles it."""
        return (2 ** self.spreading_factor) / self.bandwidth_hz * 1000.0

    @property
    def low_data_rate_optimise(self) -> bool:
        """True once a symbol outlasts 16 ms — the radio's own rule."""
        return self.symbol_time_ms > _DE_SYMBOL_THRESHOLD_MS


def params_for_preset(preset: str | None) -> ModemParams:
    """Modem parameters for a Meshtastic preset name.

    An unknown or missing name gives the default preset rather than an error:
    packets arrive from radios whose preset we were never told, and a plausible
    default beats refusing to measure.
    """
    bandwidth_khz, spreading_factor = MESHTASTIC_PRESETS.get(
        preset or "", MESHTASTIC_PRESETS[_DEFAULT_PRESET]
    )
    return ModemParams(
        spreading_factor=spreading_factor, bandwidth_hz=bandwidth_khz * 1000.0
    )


def payload_symbols(payload_bytes: int, params: ModemParams) -> int:
    """Symbols the payload occupies, preamble excluded."""
    crc = 1 if params.crc else 0
    header = 0 if params.explicit_header else 1
    optimise = 1 if params.low_data_rate_optimise else 0

    numerator = (
        8 * payload_bytes
        - 4 * params.spreading_factor
        + 28
        + 16 * crc
        - 20 * header
    )
    denominator = 4 * (params.spreading_factor - 2 * optimise)
    # max(..., 0) because a very small payload can make the numerator negative,
    # which would otherwise mean fewer than the 8 mandatory symbols.
    blocks = max(-(-numerator // denominator), 0) if numerator > 0 else 0
    return 8 + blocks * (params.coding_rate + 4)


def airtime_ms(payload_bytes: int, params: ModemParams) -> float:
    """How long one transmission of this size occupies the channel."""
    symbols = payload_symbols(payload_bytes, params) + params.preamble_symbols + 4.25
    return symbols * params.symbol_time_ms


def airtime_seconds(payload_bytes: int, params: ModemParams) -> float:
    return airtime_ms(payload_bytes, params) / 1000.0


def duty_cycle(airtime_s: float, window_s: float) -> float:
    """Fraction of a window consumed by `airtime_s` of transmissions.

    A window shorter than the traffic it contains would give a value above 1,
    which is physically impossible — a caller seeing that has mixed units or
    overlapped time ranges, so it is worth checking rather than clamping away.
    """
    if window_s <= 0:
        raise ValueError("window must be positive")
    return airtime_s / window_s
