"""Tests for RankingsPanel row pooling.

Reported bug: clicking a ranking row shortly after its 2-second refresh timer
fired could crash with "Internal C++ object (_RankRow) already deleted", and
one level up the signal chain, "Slot 'MonitorPage::_on_ranking_node_selected
(int)' not found" — both symptoms of the same root cause. update_rows() used
to destroy every row and rebuild from scratch on each call; a click could land
mid-teardown. Rows are now pooled and updated in place instead.
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv[:1])

from PySide6.QtCore import Qt  # noqa: E402
from meshchat.ui.monitor.rankings_panel import RankingsPanel  # noqa: E402


def _items(n: int, prefix: str = "node") -> list[tuple]:
    return [(i, f"{prefix}{i}", "", f"detail{i}") for i in range(n)]


class TestRowPooling:
    def test_same_widget_identity_survives_a_refresh(self):
        # The exact property that fixes the crash: a row representing the
        # same rank across two refreshes must be the same QObject, not a
        # freshly constructed (and differently-identitied) one.
        panel = RankingsPanel("A", "B")
        panel.update_rows(_items(5))
        first_pass_rows = list(panel._rows)
        panel.update_rows(_items(5))
        assert panel._rows == first_pass_rows

    def test_growing_the_result_set_adds_rows(self):
        panel = RankingsPanel("A", "B")
        panel.update_rows(_items(3))
        assert len(panel._rows) == 3
        panel.update_rows(_items(8))
        assert len(panel._rows) == 8

    def test_shrinking_the_result_set_hides_rather_than_deletes(self):
        panel = RankingsPanel("A", "B")
        panel.update_rows(_items(8))
        rows_at_peak = list(panel._rows)
        panel.update_rows(_items(2))
        # Still pooled, not destroyed — reused if a later refresh needs them.
        # isHidden() (the explicit flag we set) rather than isVisible(): the
        # panel is never shown in this test, so isVisible() would be False
        # for every widget regardless of our setVisible() calls.
        assert panel._rows == rows_at_peak
        assert not panel._rows[0].isHidden()
        assert panel._rows[5].isHidden()

    def test_hidden_rows_become_visible_again_when_reused(self):
        panel = RankingsPanel("A", "B")
        panel.update_rows(_items(8))
        panel.update_rows(_items(2))
        panel.update_rows(_items(8))
        assert not any(r.isHidden() for r in panel._rows)

    def test_row_data_reflects_the_latest_refresh(self):
        panel = RankingsPanel("A", "B")
        panel.update_rows(_items(3, prefix="old"))
        panel.update_rows(_items(3, prefix="new"))
        assert panel._rows[0]._name_lbl.text() == "new0"

    def test_click_after_many_refresh_cycles_still_works(self):
        # Simulates the reported scenario: repeated refreshes (as the 2s
        # timer would produce), then a click on a surviving row.
        panel = RankingsPanel("A", "B")
        received = []
        panel.node_selected.connect(received.append)

        for _ in range(20):
            panel.update_rows(_items(5))

        panel._rows[2].clicked.emit(panel._rows[2]._node_num)
        assert received == [2]

    def test_max_rows_caps_the_pool(self):
        panel = RankingsPanel("A", "B", max_rows=10)
        panel.update_rows(_items(100))
        assert len(panel._rows) == 10

    def test_empty_items_hides_all_rows_without_crashing(self):
        panel = RankingsPanel("A", "B")
        panel.update_rows(_items(5))
        panel.update_rows([])
        assert all(r.isHidden() for r in panel._rows)


class TestClickRacingLiveRefresh:
    """Reproduces the actual reported scenario end-to-end: a real QTimer
    driving refreshes on the Qt event loop, with a click delivered through
    Qt's own event system (not a direct method call) while refreshes are
    in flight — rather than only exercising update_rows() synchronously."""

    def test_click_survives_concurrent_timer_refreshes(self):
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtTest import QTest

        panel = RankingsPanel("A", "B")
        panel.resize(300, 200)
        panel.show()
        received = []
        panel.node_selected.connect(received.append)

        refresh_timer = QTimer()
        refresh_timer.timeout.connect(lambda: panel.update_rows(_items(6)))
        refresh_timer.start(15)  # much faster than production's 2s, on purpose

        loop = QEventLoop()
        errors = []

        def click_and_finish():
            try:
                target = panel._rows[1]
                QTest.mouseClick(target, Qt.MouseButton.LeftButton)
            except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
                errors.append(exc)
            finally:
                loop.quit()

        panel.update_rows(_items(6))
        QTimer.singleShot(200, click_and_finish)
        QTimer.singleShot(500, loop.quit)
        loop.exec()
        refresh_timer.stop()
        panel.hide()

        assert not errors, f"click raised during concurrent refresh: {errors}"
