"""Tests for the analysis panel: the five modes, the controls, and the evidence it writes.

The statistics have their own tests. These are about the parts a user actually touches — that
every mode draws something without raising, that a retune does not carry one capture's history
into another's, and that the exported files carry frequencies and seconds rather than the bin
indices the code happens to work in.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshchat.analytics.spectral_history import Burst, SpectralHistory, bursts_csv, spectrum_csv
from meshchat.ui.sigint.analysis_view import MAX_BURSTS, AnalysisMode, AnalysisView

BINS = 64
CENTRE = 906_875_000.0
SPAN = 2_000_000.0


@pytest.fixture
def view():
    widget = AnalysisView()
    widget.configure(CENTRE, SPAN, BINS)
    return widget


def _row(*, index: int | None = None, level_db: float = -50.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    values = -100.0 + rng.normal(0, 0.5, BINS)
    if index is not None:
        values[index] = level_db
    return values


class TestModes:
    def test_every_mode_draws_without_raising(self, view):
        """A mode that raises on first use is a mode nobody finds out is broken."""
        for _ in range(5):
            view.push_row(_row(index=20))
        view.push_row(_row())

        for mode in AnalysisMode:
            view.set_mode(mode)
            view._redraw()

    def test_every_mode_draws_with_no_data_at_all(self, view):
        """Starting a capture and switching mode immediately must not crash."""
        for mode in AnalysisMode:
            view.set_mode(mode)
            view._redraw()

    def test_there_are_five_modes(self):
        assert len(list(AnalysisMode)) == 5

    def test_every_mode_has_an_explanation(self, view):
        """Each mode answers a different question, so each has to say which."""
        from meshchat.ui.sigint.analysis_view import MODE_HELP

        for mode in AnalysisMode:
            assert MODE_HELP.get(mode.value), mode.value
            assert len(MODE_HELP[mode.value]) > 60, mode.value

    def test_switching_mode_updates_the_explanation(self, view):
        view.set_mode(AnalysisMode.OCCUPANCY)
        occupancy_text = view._explain.text()

        view.set_mode(AnalysisMode.CHANNELS)

        assert view._explain.text() != occupancy_text
        assert view._explain.text()

    def test_the_mode_round_trips(self, view):
        for mode in AnalysisMode:
            view.set_mode(mode)
            assert view.mode is mode


class TestFeeding:
    def test_rows_of_the_wrong_length_are_ignored(self, view):
        view.push_row(np.zeros(BINS + 1))

        assert view._history.observed == 0

    def test_rows_fold_into_the_history(self, view):
        for _ in range(4):
            view.push_row(_row(index=10))

        assert view._history.observed == 4

    def test_pushing_before_configure_does_nothing_rather_than_crashing(self):
        widget = AnalysisView()

        widget.push_row(_row())

        assert widget._history is None

    def test_a_retune_forgets_the_previous_capture(self, view):
        """The history describes a centre and span; carrying it to a new one would be a lie."""
        for _ in range(3):
            view.push_row(_row(index=10))
        assert view._history.observed == 3

        view.configure(CENTRE + 5e6, SPAN, BINS)

        assert view._history.observed == 0
        assert view._bursts == []

    def test_configure_ignores_a_nonsense_bin_count(self, view):
        view.configure(CENTRE, SPAN, 0)

        assert view._bins == BINS, "the previous geometry should stand"


class TestFreeze:
    def test_freezing_stops_new_rows_being_folded_in(self, view):
        view.push_row(_row(index=10))
        view._freeze_btn.setChecked(True)

        view.push_row(_row(index=10))
        view.push_row(_row(index=10))

        assert view._history.observed == 1

    def test_resuming_starts_folding_again(self, view):
        view._freeze_btn.setChecked(True)
        view.push_row(_row(index=10))
        view._freeze_btn.setChecked(False)

        view.push_row(_row(index=10))

        assert view._history.observed == 1

    def test_the_button_says_what_it_will_do(self, view):
        assert view._freeze_btn.text() == "Freeze"

        view._freeze_btn.setChecked(True)

        assert view._freeze_btn.text() == "Resume"
        assert view.is_frozen

    def test_freezing_does_not_discard_what_is_already_held(self, view):
        for _ in range(5):
            view.push_row(_row(index=30))

        view._freeze_btn.setChecked(True)

        assert view._history.observed == 5
        assert np.isfinite(view._history.peak_hold()[30])


class TestReset:
    def test_reset_clears_the_history_and_the_events(self, view):
        view.push_row(_row(index=10, level_db=-40.0))
        view.push_row(_row())
        assert view._history.observed == 2

        view.reset()

        assert view._history.observed == 0
        assert view._bursts == []

    def test_reset_before_any_data_is_harmless(self):
        AnalysisView().reset()


class TestEvents:
    def test_events_are_bounded(self, view):
        """A busy band makes bursts continuously; an unbounded log is a memory leak."""
        for _ in range(MAX_BURSTS + 50):
            view.push_row(_row(index=10, level_db=-40.0))
            view.push_row(_row())

        assert len(view._bursts) <= MAX_BURSTS

    def test_the_event_log_describes_itself(self, view):
        view.push_row(_row(index=10, level_db=-40.0))
        view.push_row(_row())

        lines = view.events_summary()

        assert len(lines) == 1
        assert "over floor" in lines[0]


class TestExportedEvidence:
    """Evidence that cannot be read without this program is not much evidence."""

    def _history(self) -> SpectralHistory:
        history = SpectralHistory(BINS)
        for _ in range(6):
            history.add(_row(index=12, level_db=-60.0))
        for _ in range(4):
            history.add(_row(seed=9))
        return history

    def test_the_spectrum_file_carries_frequencies_not_bin_indices(self):
        text = spectrum_csv(self._history(), centre_hz=CENTRE, span_hz=SPAN)

        header, first = text.splitlines()[0], text.splitlines()[1]
        assert header.startswith("frequency_hz\t")
        # The first bin sits half a bin above the low edge of the span.
        assert float(first.split("\t")[0]) == pytest.approx(CENTRE - SPAN / 2 + SPAN / BINS / 2,
                                                            rel=1e-6)

    def test_every_column_the_modes_draw_is_in_the_file(self):
        header = spectrum_csv(self._history(), centre_hz=CENTRE, span_hz=SPAN).splitlines()[0]

        for column in ("peak_db", "min_db", "p5_db", "p50_db", "p95_db", "occupancy"):
            assert column in header

    def test_the_file_has_one_line_per_bin(self):
        text = spectrum_csv(self._history(), centre_hz=CENTRE, span_hz=SPAN)

        assert len(text.splitlines()) == BINS + 1, "one header and one line per bin"

    def test_an_unmeasured_bin_is_written_empty_not_as_a_number(self):
        """Zero dB is a real level; a hole is not, and must not be written as one."""
        history = SpectralHistory(BINS)
        history.add(_row())

        lines = spectrum_csv(history, centre_hz=CENTRE, span_hz=SPAN).splitlines()[1:]

        assert all(line.count("\t") == 6 for line in lines)

    def test_the_event_file_carries_seconds_and_frequencies(self):
        burst = Burst(start_row=10, end_row=14, first_bin=20, last_bin=23,
                      peak_db=-55.0, floor_db=-100.0)

        text = bursts_csv([burst], centre_hz=CENTRE, span_hz=SPAN, bins=BINS, row_seconds=0.02)

        header, line = text.splitlines()[0], text.splitlines()[1]
        assert header.startswith("start_s\tend_s\t")
        fields = line.split("\t")
        assert float(fields[0]) == pytest.approx(0.20)   # 10 rows at 20 ms
        assert float(fields[1]) == pytest.approx(0.30)   # through the end of row 14
        assert 900_000_000 < float(fields[2]) < 910_000_000

    def test_an_empty_event_log_still_writes_a_header(self):
        text = bursts_csv([], centre_hz=CENTRE, span_hz=SPAN, bins=BINS, row_seconds=0.02)

        assert text.splitlines()[0].startswith("start_s")
        assert len(text.splitlines()) == 1

    def test_no_bins_does_not_divide_by_zero(self):
        text = bursts_csv([], centre_hz=CENTRE, span_hz=SPAN, bins=0, row_seconds=0.02)

        assert "start_s" in text
