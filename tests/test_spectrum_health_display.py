"""Tests for the capture-health display in the spectrum view.

The brief asked for a view that can show when it is falling behind rather than
quietly showing stale data. The arithmetic has its own tests; these are about what
the user actually sees, which is the part that makes it a feature rather than a
signal nobody reads.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal

from meshchat.services.sdr_source import measure_capture_health
from meshchat.ui.spectrum.spectrum_page import SpectrumPage

RATE = 2_400_000.0
AMBER = "#FFB800"


class _FakeController(QObject):
    """Stands in for SdrController so no thread and no dongle are involved."""

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

    def start(self, center_hz: float, rate_hz: float, gain_db: float) -> None:
        pass

    def stop(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setattr("meshchat.services.sdr_source.SdrController", _FakeController)
    page = SpectrumPage()
    # _start() is what creates and connects the controller; the dongle is a fake.
    page._start()
    return page


def _health(elapsed_s: float, received_samples: int, rows: int = 0):
    return measure_capture_health(
        received_bytes=received_samples * 2,
        sample_rate_hz=RATE,
        elapsed_s=elapsed_s,
        rows=rows,
    )


class TestHealthDisplay:
    def test_a_healthy_capture_shows_the_rate_it_is_managing(self, page):
        page._on_health(_health(10.0, int(10.0 * RATE), rows=729))

        text = page._health_lbl.text()
        assert "keeping up" in text
        assert "2.40 MS/s" in text

    def test_a_lagging_capture_is_shown_as_behind(self, page):
        page._on_health(_health(10.0, int(8.6 * RATE)))

        text = page._health_lbl.text()
        assert "behind" in text
        assert "short" in text

    def test_a_lagging_capture_is_coloured_and_a_healthy_one_is_not(self, page):
        page._on_health(_health(10.0, int(9.0 * RATE)))
        lagging_style = page._health_lbl.styleSheet()

        page._on_health(_health(10.0, int(10.0 * RATE)))
        healthy_style = page._health_lbl.styleSheet()

        assert AMBER in lagging_style
        assert AMBER not in healthy_style

    def test_a_small_shortfall_over_a_long_capture_still_warns(self, page):
        """0.5% of 100 s looks trivial as a percentage and is 1.2 M samples.

        The colouring must use the same bar as the verdict, or a capture could be
        reported as healthy in grey text while the wording says samples are missing.
        """
        page._on_health(_health(100.0, int(99.5 * RATE)))

        assert AMBER in page._health_lbl.styleSheet()

    def test_the_display_does_not_grey_out_text_that_says_it_is_behind(self, page):
        """The wording and the colour must never disagree about the same capture."""
        for health in (
            _health(10.0, int(10.0 * RATE)),
            _health(10.0, int(9.0 * RATE)),
            _health(100.0, int(99.5 * RATE)),
        ):
            page._on_health(health)

            says_behind = "behind" in page._health_lbl.text()
            is_amber = AMBER in page._health_lbl.styleSheet()
            assert says_behind == is_amber, (
                f"wording and colour disagree for {health.describe()!r}"
            )

    def test_no_figure_yet_shows_nothing_rather_than_a_zero(self, page):
        page._on_health(None)

        assert page._health_lbl.text() == ""

    def test_stopping_clears_the_last_figure(self, page):
        """A stale rate left on screen would describe a capture that has ended."""
        page._on_health(_health(10.0, int(10.0 * RATE), rows=729))

        page._stop()

        assert page._health_lbl.text() == ""

    def test_an_error_clears_the_last_figure(self, page):
        page._on_health(_health(10.0, int(10.0 * RATE)))

        page._on_error("Could not start rtl_sdr")

        assert page._health_lbl.text() == ""

    def test_the_figure_does_not_overwrite_the_lifecycle_message(self, page):
        """"Starting…" has to survive a health update arriving a moment later."""
        page._start()

        page._on_health(_health(1.0, int(1.0 * RATE)))

        assert "Starting" in page._status.text()
        assert page._health_lbl.text() != page._status.text()

    def test_health_is_displayed_in_its_own_label_never_in_status(self, page):
        page._on_health(_health(10.0, int(8.6 * RATE)))

        assert page._status.text() not in (None,)
        assert "behind" not in page._status.text()
