"""Tests for FilterBar.

Reset previously fired filter_changed up to 4 times (once per combo whose
value actually changed via currentIndexChanged, plus one explicit call) —
each one triggering a full rankings rebuild in MonitorPage. Reset should
change the underlying state exactly once as far as listeners can tell.
"""
from __future__ import annotations

from meshchat.ui.monitor.filter_bar import FilterBar


class TestFilterBarReset:
    def test_reset_emits_filter_changed_exactly_once(self):
        bar = FilterBar()
        # Move every combo away from its reset default first, so all three
        # would fire currentIndexChanged during reset if signals weren't
        # blocked.
        bar._age.setCurrentIndex(0)
        bar._src.setCurrentIndex(1)
        bar._pkt.setCurrentIndex(1)

        received = []
        bar.filter_changed.connect(received.append)
        bar._reset()

        assert len(received) == 1

    def test_reset_restores_defaults(self):
        bar = FilterBar()
        bar._age.setCurrentIndex(0)
        bar._src.setCurrentIndex(1)
        bar._pkt.setCurrentIndex(1)
        bar._reset()
        assert bar._age.currentIndex() == 2
        assert bar._src.currentIndex() == 0
        assert bar._pkt.currentIndex() == 0

    def test_reset_from_already_default_state_still_emits_once(self):
        bar = FilterBar()
        received = []
        bar.filter_changed.connect(received.append)
        bar._reset()
        assert len(received) == 1

    def test_reset_preserves_a_combo_that_was_already_blocked(self):
        # blockSignals(True) returns the *previous* blocked state — reset
        # must restore that, not unconditionally force signals back on,
        # or a combo deliberately blocked by other code becomes silently
        # un-blocked as a side effect of an unrelated Reset click.
        bar = FilterBar()
        bar._src.blockSignals(True)
        try:
            bar._reset()
            assert bar._src.signalsBlocked() is True
        finally:
            bar._src.blockSignals(False)
