"""Tests for the 3D waterfall's pure parts and its fallback behaviour.

GL rendering itself needs a context and a display, so the two functions that decide
what the picture looks like — decimation and normalisation — are tested directly,
and the widget is tested for the thing that matters most: that it degrades to a
notice instead of taking the app down when OpenGL is unavailable.
"""
from __future__ import annotations

import sys

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv[:1])

from meshchat.ui.sigint import waterfall3d_view as w3  # noqa: E402
from meshchat.ui.sigint.waterfall3d_view import (  # noqa: E402
    MAX_SURFACE_BINS,
    MAX_SURFACE_ROWS,
    Waterfall3DView,
    decimate,
    normalise,
    opengl_available,
)

#: Where a noise-only bin lands: the floor offset as a fraction of the height
#: aperture. Both are the view's own constants, kept as a number here so a change to
#: either has to be deliberate — see waterfall3d_view._FLOOR_OFFSET_DB.
_CARPET = 6.0 / 30.0


class TestDecimation:
    def test_it_thins_both_axes(self):
        history = np.zeros((300, 1024), dtype=np.float32)

        thinned = decimate(history, max_rows=96, max_bins=160)

        assert thinned.shape[0] <= 96 + 1
        assert thinned.shape[1] <= 160
        assert thinned.size < history.size

    def test_a_grid_already_small_enough_is_untouched(self):
        history = np.zeros((10, 20), dtype=np.float32)

        assert decimate(history, 96, 160).shape == (10, 20)

    def test_it_strides_rather_than_averages(self):
        """Averaging would smear a short burst until it vanished."""
        history = np.zeros((10, 4), dtype=np.float32)
        history[7] = 9.0

        thinned = decimate(history, max_rows=5, max_bins=4)

        assert thinned.max() == pytest.approx(9.0), "the peak survives intact"
        assert 9.0 in thinned

    def test_the_newest_row_is_kept(self):
        """The bottom row is the most recent, so it must never be thinned away."""
        history = np.zeros((10, 4), dtype=np.float32)
        history[-1] = 5.0

        thinned = decimate(history, max_rows=3, max_bins=4)

        assert thinned[-1].max() == pytest.approx(5.0), "the newest row survives"

    def test_an_empty_history_is_returned_as_is(self):
        empty = np.zeros((0, 0), dtype=np.float32)

        assert decimate(empty, 96, 160).size == 0

    def test_silly_limits_do_not_divide_by_zero(self):
        history = np.zeros((10, 4), dtype=np.float32)

        assert decimate(history, max_rows=0, max_bins=0).shape == (10, 4)

    def test_the_result_is_contiguous(self):
        """pyqtgraph hands these straight to GL buffers."""
        history = np.arange(300 * 1024, dtype=np.float32).reshape(300, 1024)

        assert decimate(history, 96, 160).flags["C_CONTIGUOUS"]

    def test_the_defaults_stay_within_the_display_budget(self):
        history = np.zeros((300, 1024), dtype=np.float32)

        thinned = decimate(history, MAX_SURFACE_ROWS, MAX_SURFACE_BINS)

        assert thinned.shape[0] <= MAX_SURFACE_ROWS + 1
        assert thinned.shape[1] <= MAX_SURFACE_BINS


class TestNormalisation:
    def test_it_spans_zero_to_one(self):
        power = np.linspace(-80.0, -20.0, 1000).reshape(10, 100)

        scaled = normalise(power)

        assert scaled.min() >= 0.0
        assert scaled.max() <= 1.0
        assert scaled.max() > 0.9

    def test_a_loud_outlier_does_not_squash_the_real_structure(self):
        """The reason the floor is a percentile instead of min/max.

        The claim is not only that the outlier reaches the top — it is that one loud
        bin leaves the rest of the picture exactly where it was.
        """
        rng = np.random.default_rng(11)
        power = rng.normal(-80.0, 3.0, (20, 100))
        with_outlier = power.copy()
        with_outlier[0, 0] = 40.0

        scaled = normalise(with_outlier)
        reference = normalise(power)

        assert scaled[0, 0] == pytest.approx(1.0)
        assert np.allclose(scaled[1:], reference[1:], atol=0.05)

    def test_an_outlier_in_a_flat_buffer_stands_on_its_own(self):
        """Everything on the carpet and one value at the top is what it is."""
        power = np.full((10, 100), -80.0)
        power[0, 0] = 40.0

        scaled = normalise(power)

        assert scaled[0, 0] == pytest.approx(1.0)
        assert np.allclose(scaled[1:], _CARPET)

    def test_a_flat_buffer_sits_on_the_noise_carpet(self):
        """No spread means the floor — and the floor is deliberately not at zero.

        See _FLOOR_OFFSET_DB: 6 dB up a 30 dB scale is a fifth of the height, which
        is the thickness that makes the surface read as a surface.
        """
        scaled = normalise(np.full((4, 4), -50.0))

        assert np.allclose(scaled, _CARPET)

    def test_a_noise_only_band_stays_flat(self):
        """The fault this mapping exists to prevent.

        On a band with no signals the data's own spread is the noise. Fitting the
        height to it stretched a 3 dB spread over the whole surface and every bin
        became a full-height needle, so the 3D view read as a hedge rather than a
        spectrum. Even four sigma of noise is not a signal.
        """
        rng = np.random.default_rng(7)
        power = rng.normal(-95.0, 3.0, (96, 160)).astype(np.float32)

        scaled = normalise(power)

        assert np.median(scaled) == pytest.approx(0.28, abs=0.06), "noise sits on the carpet"
        assert scaled.max() < 0.75, "even four sigma of noise is not full scale"

    def test_a_signal_twenty_db_over_the_floor_stands_up(self):
        """A carrier is a ridge you can read off the noise, not a needle.

        Measured across its rows rather than at one row: an individual row of a
        steady carrier still moves by a few dB of noise, so the height of the
        ridge is what carries the signal.
        """
        rng = np.random.default_rng(7)
        power = rng.normal(-95.0, 3.0, (96, 160)).astype(np.float32)
        power[:, 80] += 20.0

        scaled = normalise(power)

        carrier = scaled[:, 80]
        assert carrier.mean() > 0.6
        assert carrier.mean() > scaled[:, 10].max(), "the carrier clears the noise"

    def test_nan_does_not_poison_the_scaling(self):
        power = np.linspace(-80.0, -20.0, 100).reshape(10, 10)
        power[3, 3] = np.nan

        scaled = normalise(power)

        assert np.isfinite(scaled).all()

    def test_an_all_nan_buffer_is_all_zero(self):
        scaled = normalise(np.full((4, 4), np.nan))

        assert np.allclose(scaled, 0.0)

    def test_an_empty_buffer_is_all_zero(self):
        assert normalise(np.zeros((0, 0))).size == 0

    def test_it_returns_float32_for_gl(self):
        assert normalise(np.arange(100, dtype=np.float64).reshape(10, 10)).dtype == np.float32


class TestAvailability:
    def test_it_reports_a_reason_in_both_directions(self):
        available, reason = opengl_available()

        assert isinstance(available, bool)
        assert reason, "a caller needs something to show either way"

    def test_a_missing_package_is_named(self, monkeypatch):
        """The most likely failure on a fresh machine."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "OpenGL":
                raise ImportError("no OpenGL")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        available, reason = opengl_available()

        assert available is False
        assert "pip install PyOpenGL" in reason


class TestWidgetFallback:
    def test_constructing_it_creates_no_gl_context(self):
        """The regression guard.

        A GL widget forces native OpenGL composition on its top-level window, and
        the map's QWebEngineView needs Qt Quick's composition — with the surface
        built inside the main window the map could not render at all. So
        construction must not make one.
        """
        view = Waterfall3DView()

        assert view._surface is None
        assert view._gl is None
        assert view.active is False

    def test_activating_without_opengl_shows_a_notice(self, monkeypatch):
        monkeypatch.setattr(w3, "opengl_available", lambda: (False, "no GL here"))

        view = Waterfall3DView()
        activated = view.activate()

        assert activated is False
        assert view.available is False
        assert "no GL here" in view.unavailable_reason
        assert view._notice is not None

    def test_the_pushes_all_configure_calls_are_safe_without_opengl(self, monkeypatch):
        monkeypatch.setattr(w3, "opengl_available", lambda: (False, "no GL here"))
        view = Waterfall3DView()
        view.activate()

        view.configure(915e6, 2.4e6, 1024)
        view.push_row(np.zeros(1024, dtype=np.float32))
        view.clear()
        view.stop()

    def test_an_unavailable_view_says_so_rather_than_showing_nothing(
        self, monkeypatch
    ):
        monkeypatch.setattr(w3, "opengl_available", lambda: (False, "no GL here"))

        view = Waterfall3DView()
        view.activate()

        assert view._notice is not None
        assert "unavailable" in view._notice.text().lower()

    def test_rows_are_buffered_even_with_no_surface(self, monkeypatch):
        """So opening the window mid-capture shows history, not a blank grid."""
        monkeypatch.setattr(w3, "opengl_available", lambda: (False, "no GL here"))
        view = Waterfall3DView()
        view.configure(915e6, 2.4e6, 4)

        view.push_row(np.full(4, 7.0, dtype=np.float32))

        assert view._history is not None
        assert view._history[-1, 0] == pytest.approx(7.0)


@pytest.mark.skipif(not opengl_available()[0], reason="OpenGL not available here")
class TestWidgetWithOpenGL:
    def test_it_builds_a_surface_only_when_asked(self):
        view = Waterfall3DView()

        assert view.available is True
        assert view.active is False

        assert view.activate() is True
        assert view.active is True
        assert view._surface is not None
        assert view._gl is not None
        view.stop()

    def test_activating_twice_is_harmless(self):
        view = Waterfall3DView()

        assert view.activate() is True
        surface = view._surface
        assert view.activate() is True

        assert view._surface is surface, "the surface is reused, not rebuilt"
        view.stop()

    def test_it_accepts_a_realistic_capture(self):
        view = Waterfall3DView()
        assert view.activate() is True
        view.configure(915e6, 2.4e6, 1024)

        for index in range(200):
            view.push_row(np.full(1024, -60.0 + index % 7, dtype=np.float32))

        assert view._redraw() is True, "a redraw must actually reach the surface"
        view.stop()

    def test_the_surface_data_matches_the_axes(self):
        """pyqtgraph wants z[i, j] for x[i], y[j] — so the grid must be transposed.

        Getting this wrong raises on every frame and leaves the surface empty;
        the redraw used to swallow that, so this checks the shapes directly.
        """
        import pyqtgraph.opengl as gl

        captured = {}
        original = gl.GLSurfacePlotItem.setData

        def spy(self, **kwargs):
            captured.update(kwargs)
            return original(self, **kwargs)

        view = Waterfall3DView()
        assert view.activate() is True
        view.configure(915e6, 2.4e6, 64)
        for index in range(40):
            view.push_row(np.full(64, float(index), dtype=np.float32))

        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as patch:
            patch.setattr(gl.GLSurfacePlotItem, "setData", spy)
            assert view._redraw() is True

        assert captured, "setData was never called"
        z = captured["z"]
        assert z.shape == (len(captured["x"]), len(captured["y"])), (
            "z must be (len(x), len(y))"
        )
        assert len(captured["colors"]) == z.size
        view.stop()

    def test_rows_of_the_wrong_size_are_ignored(self):
        """A geometry change between configure and push must not corrupt the grid."""
        view = Waterfall3DView()
        view.configure(915e6, 2.4e6, 1024)

        view.push_row(np.zeros(64, dtype=np.float32))

        assert view._history is not None
        assert not view._history.any()

    def test_pushing_before_configure_does_nothing(self):
        view = Waterfall3DView()

        view.push_row(np.zeros(1024, dtype=np.float32))

    def test_redrawing_before_configure_does_nothing(self):
        view = Waterfall3DView()

        view._redraw()

    def test_the_history_keeps_the_newest_rows(self):
        view = Waterfall3DView()
        view.configure(915e6, 2.4e6, 8)
        for index in range(5):
            view.push_row(np.full(8, float(index), dtype=np.float32))

        assert view._history is not None
        assert view._history[-1, 0] == pytest.approx(4.0)
        assert view._history[-2, 0] == pytest.approx(3.0)
