"""Tests for the override display, and for what it must not claim.

The signal's own tests cover the transmit path. These are about the words on screen, because
the wording is where an unverified claim would end up: measured on this board, every transmit
register reads back exactly as written while the spectrum stays unchanged, so a label reading
"ON AIR" would be asserting something no measurement here supports.
"""
from __future__ import annotations

from meshchat.services.rf import pluto_tx
from meshchat.ui.sigint.override_control import OverrideWarning


class TestWarningText:
    """The warning is read before anything is transmitted, so it carries the real picture."""

    def test_it_names_the_radio_that_will_be_blinded(self):
        text = OverrideWarning.text(OverrideWarning.request())

        assert "channel 19" in text
        assert "unable to receive" in text

    def test_it_says_how_long_the_carrier_lasts(self):
        text = OverrideWarning.text(OverrideWarning.request())

        assert "30 seconds" in text

    def test_it_says_the_mechanism_is_overload_not_a_filled_channel(self):
        """The distinction that decides how to read the result.

        A success from proximity looks identical to a success from spectral coverage unless
        the person running it knows which one they are testing.
        """
        text = OverrideWarning.text(OverrideWarning.request())

        assert "receiver overload" in text
        assert "does not predict" in text

    def test_it_says_the_carrier_can_be_stopped_by_hand(self):
        text = OverrideWarning.text(OverrideWarning.request())

        assert "until you stop it" in text

    def test_it_does_not_claim_the_tone_will_work(self):
        text = OverrideWarning.text(OverrideWarning.request())

        assert "will be unable" in text  # about the radios, which is the intent
        assert "jammed" not in text.lower()
        assert "guaranteed" not in text.lower()

    def test_it_reports_the_drive_as_amplitude(self):
        """75% of amplitude is -2.5 dB; 75% of power would be -1.25 dB."""
        text = OverrideWarning.text(OverrideWarning.request())

        assert "75% drive" in text
        assert "-2.5 dB" in text

    def test_it_says_the_cap_is_what_the_request_actually_carries(self):
        """The warning is built from the request, so it cannot describe a different tone."""
        text = OverrideWarning.text(pluto_tx.ToneRequest(frequency_hz=906_875_000.0,
                                                         amplitude=0.5))

        assert "50% drive" in text


class TestRequest:
    def test_the_button_sends_the_frequency_the_user_asked_for(self):
        assert OverrideWarning.request().frequency_hz == 906_875_000.0

    def test_the_button_sends_for_thirty_seconds(self):
        assert OverrideWarning.request().duration_s == 30.0

    def test_the_request_is_below_the_ceiling(self):
        request = OverrideWarning.request()

        assert request.amplitude <= pluto_tx.MAX_TX_DRIVE
        assert request.amplitude < 0.90
