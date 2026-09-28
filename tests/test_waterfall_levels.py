"""Tests for the waterfall's display range.

This is the control that fixes a saturated display, so it gets the same treatment
as the DSP: the fitting is driven with known data and the numbers checked.
"""
from __future__ import annotations

import sys

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv[:1])

from meshchat.ui.spectrum.waterfall_view import (  # noqa: E402
    MIN_LEVELS_SPAN_DB,
    WaterfallView,
)
from meshchat.ui.widgets.levels_control import LevelsControl  # noqa: E402

_CENTER_HZ = 915.0e6
_SPAN_HZ = 2.4e6
_BINS = 1024


def _view() -> WaterfallView:
    view = WaterfallView()
    view.configure(_CENTER_HZ, _SPAN_HZ, _BINS)
    return view


def _feed(view: WaterfallView, centre_db: float, spread_db: float = 1.0, rows: int = 40) -> None:
    rng = np.random.default_rng(99)
    for _ in range(rows):
        view.push_row(rng.normal(centre_db, spread_db, _BINS).astype(np.float32))


class TestAutoFitting:
    def test_levels_are_set_before_the_first_paint(self):
        """pyqtgraph cannot render float data with levels=None.

        `setImage(autoLevels=False)` leaves levels unset, so skipping setLevels
        until the auto-fit ran meant the first paint raised "levels argument is
        required for float input types" — a crash in the window that no test
        caught, because tests do not paint.
        """
        view = _view()

        assert view._image.levels is not None

    def test_the_first_row_of_data_also_leaves_levels_set(self):
        view = _view()

        view.push_row(np.full(_BINS, -30.0, dtype=np.float32))

        assert view._image.levels is not None

    def test_the_image_can_be_rendered_with_the_levels_it_holds(self):
        """Reproduces the crash without needing a paint.

        ImageItem.render() calls makeARGB with its own levels; passing None
        raises "levels argument is required for float input types", which is
        what took the window down.
        """
        from pyqtgraph import functions as fn

        view = _view()
        view.push_row(np.full(_BINS, -5.0, dtype=np.float32))

        argb = fn.makeARGB(view._history, levels=view._image.levels)

        assert argb[0].shape[0] == view._history.shape[0]

    def test_a_high_level_band_is_not_left_saturated(self):
        """The reported fault: everything above the top of the scale.

        The view used to draw against a fixed -110..-20 dB window while the
        capture path produces values near +20, so every bin clipped to the top
        colour and the panel became a solid block of it.
        """
        view = _view()

        _feed(view, centre_db=20.0)

        low, high = view.levels()
        assert low < 20.0 < high, "the actual signal must fall inside the window"
        assert not (low > 20.0 or high < 20.0)

    def test_a_low_level_band_is_fitted_too(self):
        view = _view()

        _feed(view, centre_db=-70.0)

        low, high = view.levels()
        assert low < -70.0 < high

    def test_the_window_is_tight_enough_to_show_structure(self):
        """Wider than needed and a quiet band still looks flat."""
        view = _view()

        _feed(view, centre_db=10.0, spread_db=1.0)

        low, high = view.levels()
        assert high - low < 40.0

    def test_a_flat_band_gets_the_minimum_span_not_a_zero_one(self):
        """Otherwise a noise-free input would divide by an empty range."""
        view = _view()
        for _ in range(20):
            view.push_row(np.full(_BINS, 5.0, dtype=np.float32))

        low, high = view.levels()

        assert high - low == pytest.approx(MIN_LEVELS_SPAN_DB)
        assert low < 5.0 < high

    def test_one_loud_bin_does_not_flatten_everything_else(self):
        view = _view()
        rng = np.random.default_rng(4)
        for _ in range(30):
            row = rng.normal(-60.0, 0.5, _BINS).astype(np.float32)
            row[10] = 40.0
            view.push_row(row)

        low, high = view.levels()

        assert high < 20.0, "the outlier must not drag the top of the window up"

    def test_it_emits_when_the_range_changes(self):
        view = _view()
        seen: list[tuple[float, float]] = []
        view.levels_changed.connect(lambda low, high: seen.append((low, high)))

        _feed(view, centre_db=25.0)

        assert seen, "a consumer needs to know the range moved"
        assert seen[-1] == view.levels()

    def test_a_stable_band_does_not_repaint_the_same_window(self):
        """Re-fitting every row would make the picture breathe."""
        view = _view()
        for _ in range(30):
            view.push_row(np.full(_BINS, 12.0, dtype=np.float32))
        settled = view.levels()

        changes = []
        view.levels_changed.connect(lambda low, high: changes.append((low, high)))
        # No new data: a scheduled re-fit must decide there is nothing to change.
        view.fit_levels()

        assert view.levels() == settled
        assert changes == []

    def test_fitting_before_any_data_does_nothing(self):
        view = _view()

        view.fit_levels()

        assert view.levels()  # still a usable window, just not fitted


class TestManualControl:
    def test_auto_is_on_by_default(self):
        assert _view().auto_levels is True

    def test_manual_limits_are_used_verbatim(self):
        view = _view()
        view.set_auto_levels(False)

        view.set_levels(-30.0, 5.0)

        assert view.levels() == (-30.0, 5.0)

    def test_manual_limits_survive_incoming_data(self):
        """Turning auto off must actually stop it re-fitting."""
        view = _view()
        view.set_auto_levels(False)
        view.set_levels(-30.0, 5.0)

        _feed(view, centre_db=40.0)

        assert view.levels() == (-30.0, 5.0)

    def test_an_inverted_range_is_refused(self):
        view = _view()

        with pytest.raises(ValueError):
            view.set_levels(10.0, -10.0)

    def test_turning_auto_back_on_re_fits(self):
        view = _view()
        view.set_auto_levels(False)
        view.set_levels(-200.0, 200.0)

        _feed(view, centre_db=15.0)
        view.set_auto_levels(True)

        low, high = view.levels()
        assert low < 15.0 < high
        assert high - low < 100.0, "it fitted rather than keeping the huge window"

    def test_a_new_geometry_fits_again_from_scratch(self):
        """A different capture may be at a different gain."""
        view = _view()
        _feed(view, centre_db=20.0)

        view.configure(_CENTER_HZ, _SPAN_HZ, _BINS)
        _feed(view, centre_db=-60.0)

        low, high = view.levels()
        assert low < -60.0 < high


class TestResetView:
    def test_it_restores_the_captured_span(self):
        view = _view()
        view._plot.setXRange(0.0, 1.0)

        view.reset_view()

        left, right = view._plot.getViewBox().viewRange()[0]
        # The plot works in Hz, not MHz: the axis label carries the unit.
        assert left == pytest.approx(_CENTER_HZ - _SPAN_HZ / 2, rel=1e-6)
        assert right - left == pytest.approx(_SPAN_HZ, rel=1e-3)

    def test_resetting_before_a_capture_does_nothing(self):
        WaterfallView().reset_view()


class TestLevelsControl:
    def test_it_starts_in_auto(self):
        control = LevelsControl(_view())

        assert control._auto.isChecked() is True
        assert control._low.isEnabled() is False

    def test_it_mirrors_the_views_range(self):
        view = _view()
        control = LevelsControl(view)
        view.set_auto_levels(False)

        view.set_levels(-25.0, 15.0)

        assert control._low.value() == pytest.approx(-25.0)
        assert control._high.value() == pytest.approx(15.0)

    def test_unchecking_auto_enables_the_limits(self):
        control = LevelsControl(_view())
        control._auto.setChecked(False)

        assert control._low.isEnabled() is True
        assert control._high.isEnabled() is True

    def test_editing_the_limits_sets_the_views_range(self):
        view = _view()
        control = LevelsControl(view)
        control._auto.setChecked(False)

        control._low.setValue(-40.0)
        control._high.setValue(0.0)

        assert view.levels() == pytest.approx((-40.0, 0.0))

    def test_an_inverted_manual_entry_is_corrected_rather_than_ignored(self):
        """A display with no range is not a state to leave the user in."""
        view = _view()
        control = LevelsControl(view)
        control._auto.setChecked(False)
        control._low.setValue(0.0)

        control._high.setValue(-10.0)

        low, high = view.levels()
        assert high > low

    def test_re_checking_auto_fits_again(self):
        view = _view()
        control = LevelsControl(view)
        control._auto.setChecked(False)
        control._high.setValue(200.0)
        control._low.setValue(-200.0)

        control._auto.setChecked(True)
        _feed(view, centre_db=5.0)
        view.fit_levels()

        low, high = view.levels()
        assert high - low < 100.0

    def test_the_fit_button_forces_a_re_fit(self):
        view = _view()
        control = LevelsControl(view)
        control._auto.setChecked(False)
        view.set_levels(-300.0, 300.0)
        _feed(view, centre_db=5.0)

        control._fit.click()

        low, high = view.levels()
        assert high - low < 100.0

    def test_the_reset_button_restores_the_span(self):
        view = _view()
        control = LevelsControl(view)
        view._plot.setXRange(0.0, 1.0)

        control._reset.click()

        left, right = view._plot.getViewBox().viewRange()[0]
        assert right - left == pytest.approx(_SPAN_HZ, rel=1e-3)
