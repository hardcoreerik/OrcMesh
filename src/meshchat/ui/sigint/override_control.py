"""The 906.875 MHz override button: a transmit control, and the warning that guards it.

This is the only control in OrcMesh that deliberately interferes with other radios, so it is
built to be hard to trigger by accident and easy to stop:

* **A warning before anything goes out**, naming the consequences rather than saying "are you
  sure". The user reads it before the button does anything, and the confirm button is the
  only one that transmits.
* **The button becomes the stop button** while a tone is on the air, so the control that
  started it is the control that ends it. There is no state where a tone is running and no
  visible way to stop it.
* **A countdown**, so the remaining time is never a guess.
* **It stops on its own**, at the requested duration and also from a watchdog inside the
  transmitter, so a UI fault cannot leave a carrier on the air.

The channel is not a coincidence: 906.875 MHz is US Meshtastic channel slot 19
(902 + 0.125 + 19 x 0.25), which is where the local mesh sits. Overriding the local mesh is
the entire point of the experiment.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from meshchat.services import rf
from meshchat.services.rf import pluto_tx

log = logging.getLogger(__name__)

#: The channel slot this button targets, and the reason it is this one.
OVERRIDE_FREQUENCY_HZ = 906_875_000.0
OVERRIDE_DURATION_S = 30.0

#: How often the countdown updates. Fast enough to look live, slow enough not to matter.
_TICK_MS = 200


class OverrideWarning:
    """Builds the text shown before transmitting, and asks for one deliberate confirmation."""

    @staticmethod
    def request() -> pluto_tx.ToneRequest:
        """The one tone this button sends, in one place."""
        return pluto_tx.ToneRequest(
            frequency_hz=OVERRIDE_FREQUENCY_HZ, duration_s=OVERRIDE_DURATION_S,
        )

    @staticmethod
    def text(request: pluto_tx.ToneRequest) -> str:
        """The warning, in the user's terms rather than the hardware's.

        Names the radio that will be blinded, says how long, and says what the mechanism
        actually is — because "jamming by filling the channel" and "jamming by overloading
        the receiver" are different experiments, and someone running this should know which
        one they are running.
        """
        return (
            f"<b>Transmit a continuous carrier on 906.875 MHz for "
            f"{request.duration_s:g} seconds?</b>"
            f"<p>This drives the Pluto's <b>TX1</b> output at "
            f"{request.amplitude * 100:.0f}% drive ({request.amplitude_db:+.1f} dB)."
            f"<p>While it is on:"
            "<ul>"
            "<li><b>every Meshtastic node in range will be unable to receive on channel 19</b>"
            " — including your own radios, which is the intent of the test</li>"
            "<li>anything else within range listening on 906.875 MHz is affected too</li>"
            "<li>the carrier stays on until you stop it or the "
            f"{request.duration_s:g} seconds expire</li>"
            "</ul>"
            "<p>The mechanism at bench range is <b>receiver overload, not a filled channel</b>:"
            " a metre away the Pluto lands roughly 90 dB above LoRa's sensitivity floor, so"
            " nearby radios are desensitised rather than spectrally covered. Success here does"
            " not predict what a distant interferer would achieve."
            "<p>A continuous carrier is not the modulation the ISM band rules are written"
            " around, so check you are authorised to transmit before confirming."
        )

    @staticmethod
    def confirm(parent) -> bool:
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Transmit on 906.875 MHz")
        box.setText(OverrideWarning.text(OverrideWarning.request()))
        transmit = box.addButton("Transmit now", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        # Cancel is the default, so an Enter keypress or a stray double-tap cannot transmit.
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is transmit


class OverrideController:
    """Owns the transmitter and the countdown for one SIGINT page.

    Kept out of the widget so the transmit logic can be tested without a window, and so the
    widget has no way to transmit that does not go through this.
    """

    def __init__(self, *, on_state=None, uri_finder=None) -> None:
        self._transmitter: pluto_tx.ToneTransmitter | None = None
        self._timer = QTimer()
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._on_state = on_state
        self._find_uri = uri_finder or pluto_uri
        self._remaining_s = 0.0

    @property
    def is_transmitting(self) -> bool:
        return self._transmitter is not None and self._transmitter.is_transmitting

    @property
    def remaining_s(self) -> float:
        return max(0.0, self._remaining_s)

    def _publish(self) -> None:
        if self._on_state is not None:
            self._on_state(self)

    def start(self) -> pluto_tx.TonePlan:
        """Find the Pluto, then transmit. Raises with a reason if either step fails."""
        uri = self._find_uri()
        if uri is None:
            raise pluto_tx.ToneFailed(
                "No Pluto SDR was found. The tone comes from the Pluto's TX1 output, so it "
                "cannot run without it — check the board is powered and on the network.",
            )
        transmitter = pluto_tx.ToneTransmitter(uri)
        plan = transmitter.start(OverrideWarning.request())
        self._transmitter = transmitter
        self._remaining_s = plan.request.duration_s
        self._timer.start()
        self._publish()
        return plan

    def stop(self) -> None:
        """End the tone. Safe to call when nothing is transmitting."""
        self._timer.stop()
        transmitter, self._transmitter = self._transmitter, None
        self._remaining_s = 0.0
        if transmitter is not None:
            transmitter.stop()
        self._publish()

    def _tick(self) -> None:
        self._remaining_s -= _TICK_MS / 1000.0
        if self._remaining_s <= 0.0:
            log.info("Override tone reached its duration")
            self.stop()
            return
        self._publish()

    def shutdown(self) -> None:
        """Silence the transmitter. Called from the page's own shutdown path."""
        self.stop()


def pluto_uri() -> str | None:
    """The best URI for the Pluto, or None if it is not attached.

    Asked of the RF registry rather than hardcoded, so a board that moves to a different
    address — or is reached over USB instead — still works. Discovery runs `iio_info -s`,
    which costs a few seconds, so this is only ever called on a deliberate button press.
    """
    try:
        devices, _ = rf.discover()
    except OSError:
        log.exception("Could not look for a Pluto")
        return None
    for device in devices:
        transport = device.streaming_transport()
        if transport is not None:
            return transport.uri
    return None


__all__ = [
    "OVERRIDE_DURATION_S",
    "OVERRIDE_FREQUENCY_HZ",
    "OverrideController",
    "OverrideWarning",
    "pluto_uri",
]
