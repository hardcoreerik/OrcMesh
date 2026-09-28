"""Tests for the Spectrum page's gain control and display-range control.

The gain used to be hardcoded to automatic, which on an RTL2832U resolves to
near-maximum — the nearest thing to a cause of a saturated display after the
display range itself. Both are now controls, so both are pinned here.
"""
from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv[:1])

from meshchat.services import rtl_tools  # noqa: E402
from meshchat.ui.spectrum.spectrum_page import SpectrumPage  # noqa: E402
from meshchat.ui.widgets.levels_control import LevelsControl  # noqa: E402


class _FakeController(QObject):
    """Stands in for SdrController so no thread or dongle is involved."""

    row_ready = Signal(object)
    started = Signal(float, float, int)
    stopped = Signal(str)
    error = Signal(str)
    recording_finished = Signal(object)
    recording_failed = Signal(str)
    health = Signal(object)

    def __init__(self, parent=None, *, label: str = ""):
        super().__init__(parent)
        self.label = label
        self.started_with: list[tuple[float, float, float]] = []

    def start(self, center_hz: float, rate_hz: float, gain_db: float) -> None:
        self.started_with.append((center_hz, rate_hz, gain_db))

    def stop(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setattr(
        "meshchat.services.sdr_source.SdrController", _FakeController
    )
    return SpectrumPage()


class TestGainControl:
    def test_the_gain_is_not_automatic(self, page):
        """Auto resolves to near-maximum, which is the worst case for headroom."""
        assert page._gain.value() > 0.0

    def test_it_defaults_to_the_shared_default(self, page):
        assert page._gain.value() == pytest.approx(rtl_tools.DEFAULT_GAIN_DB)

    def test_the_tooltip_lists_the_tuners_own_steps(self, page):
        """The tuner snaps to its table, so offering anything else misleads."""
        tooltip = page._gain.toolTip()

        assert "15.7" in tooltip
        assert "28" in tooltip

    def test_the_chosen_gain_reaches_the_capture(self, page):
        page._gain.setValue(28.0)

        page._start()

        assert page._sdr is not None
        assert page._sdr.started_with[0][2] == pytest.approx(28.0)

    def test_a_low_gain_is_passed_through_too(self, page):
        page._gain.setValue(3.7)

        page._start()

        assert page._sdr is not None
        assert page._sdr.started_with[0][2] == pytest.approx(3.7)

    def test_the_capture_geometry_still_comes_from_the_page(self, page):
        page._center.setValue(918.5)
        page._rate.setValue(2.56)

        page._start()

        assert page._sdr is not None
        center_hz, rate_hz, _gain = page._sdr.started_with[0]
        assert center_hz == pytest.approx(918.5e6)
        assert rate_hz == pytest.approx(2.56e6)


class TestLevelsControlIsPresent:
    def test_the_page_has_a_display_range_control(self, page):
        controls = page.findChildren(LevelsControl)

        assert len(controls) == 1

    def test_it_is_wired_to_this_pages_waterfall(self, page):
        control = page.findChildren(LevelsControl)[0]

        assert control._view is page._waterfall

    def test_the_waterfall_fits_its_own_range_by_default(self, page):
        assert page._waterfall.auto_levels is True
