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


class TestCustomFrequencyMarker:
    """Covers fixed-frequency-override meshes (e.g. MeshOregon at 918.5 MHz)
    that don't sit on any channel/preset-derived slot, so no region/preset
    math could ever surface them — the user has to mark them manually."""

    def test_add_custom_marker_appears_in_current_markers(self):
        page = SpectrumPage()
        page._custom_label.setText("MeshOregon")
        page._custom_freq.setValue(918.5)
        page._custom_bw.setValue(125.0)
        page._on_add_custom_marker()

        labels = [m.label for m in page._current_markers()]
        assert "MeshOregon" in labels
        marker = next(m for m in page._current_markers() if m.label == "MeshOregon")
        assert marker.center_mhz == 918.5
        assert marker.bandwidth_khz == 125.0

    def test_add_custom_marker_without_label_uses_frequency(self):
        page = SpectrumPage()
        page._custom_label.setText("")
        page._custom_freq.setValue(906.0)
        page._on_add_custom_marker()

        labels = [m.label for m in page._current_markers()]
        assert "906.000 MHz" in labels

    def test_clear_custom_markers_removes_them(self):
        page = SpectrumPage()
        page._custom_freq.setValue(918.5)
        page._on_add_custom_marker()
        assert page._custom_markers

        page._on_clear_custom_markers()
        assert page._custom_markers == []
        assert all(m.center_mhz != 918.5 for m in page._current_markers())

    def test_custom_label_does_not_hijack_the_active_recenter_target(self):
        # A custom marker labelled "Active override" must not be mistaken
        # for the computed active-slot marker by the "active" substring
        # heuristic in _on_band_changed — that heuristic must only look at
        # base (region/preset-computed) markers, not user-typed ones.
        page = SpectrumPage()
        idx = page._band.findData("US")
        page._band.setCurrentIndex(idx)
        expected_center = page._center.value()

        page._custom_label.setText("Active override")
        page._custom_freq.setValue(918.5)
        page._on_add_custom_marker()

        page._on_band_changed(page._band.currentIndex())
        assert page._center.value() == expected_center

    def test_custom_label_does_not_hijack_the_status_line(self):
        page = SpectrumPage()
        idx = page._band.findData("US")
        page._band.setCurrentIndex(idx)
        original_marker_text = page._marker_lbl.text()

        page._custom_label.setText("nominal test")
        page._custom_freq.setValue(918.5)
        page._on_add_custom_marker()

        assert page._marker_lbl.text() == original_marker_text
