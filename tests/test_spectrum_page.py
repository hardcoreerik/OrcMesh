"""Tests for SpectrumPage's band-selection out-of-range warning.

QDoubleSpinBox.setValue() silently clamps to its configured range with no
error — selecting the 2.4 GHz Meshtastic region (center ~2441.75 MHz)
against the 24-1766 MHz range this RTL-SDR tuner chip actually supports
used to display 1766 MHz with no indication anything went wrong.
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv[:1])

from meshchat.ui.spectrum.spectrum_page import SpectrumPage  # noqa: E402


class TestBandOutOfRangeWarning:
    def test_selecting_2_4ghz_region_shows_a_warning(self):
        page = SpectrumPage()
        idx = page._band.findData("LORA_24")
        assert idx >= 0, "LORA_24 should be a selectable Meshtastic region"
        page._band.setCurrentIndex(idx)
        assert page._band_warning_lbl.text() != ""
        assert "outside" in page._band_warning_lbl.text().lower()

    def test_selecting_an_in_range_region_clears_the_warning(self):
        page = SpectrumPage()
        idx_24 = page._band.findData("LORA_24")
        page._band.setCurrentIndex(idx_24)
        assert page._band_warning_lbl.text() != ""

        idx_us = page._band.findData("US")
        page._band.setCurrentIndex(idx_us)
        assert page._band_warning_lbl.text() == ""

    def test_band_change_does_not_touch_unrelated_status_label(self):
        # self._status is used for SDR availability / capture-lifecycle
        # messages, unrelated to band selection — a band change must not
        # silently overwrite whatever it was already showing.
        page = SpectrumPage()
        page._status.setText("Capturing — 2 dB noise floor")
        idx = page._band.findData("LORA_24")
        page._band.setCurrentIndex(idx)
        assert page._status.text() == "Capturing — 2 dB noise floor"

    def test_in_range_region_sets_the_center_frequency(self):
        page = SpectrumPage()
        idx = page._band.findData("US")
        page._band.setCurrentIndex(idx)
        assert page._center.minimum() <= page._center.value() <= page._center.maximum()
