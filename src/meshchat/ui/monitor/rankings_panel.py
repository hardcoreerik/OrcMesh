"""MeshChat – RankingsPanel: Last Heard / Most Packets / Nearby / Big Signal / Messages Sent."""
from __future__ import annotations

import logging

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


log = logging.getLogger(__name__)


class _RankRow(QWidget):
    """One row. Reused across refreshes via set_data() rather than being
    destroyed and recreated — see the note on RankingsPanel.update_rows()."""

    # object, not int: Meshtastic node_num is a 32-bit unsigned value and
    # can exceed 0x7FFFFFFF — a Qt-typed int signal is C++ int32 and
    # silently wraps it to negative (see ARCHITECTURE.md / the map-pin
    # click chain, which had the same bug).
    clicked = Signal(object)  # node_num

    def __init__(self, parent=None):
        super().__init__(parent)
        self._node_num: int | None = None
        self.setObjectName("rankItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self._rank_lbl = QLabel()
        self._rank_lbl.setStyleSheet("color: #3A4870; font-size: 10px; min-width: 16px;")
        layout.addWidget(self._rank_lbl)

        self._name_lbl = QLabel()
        self._name_lbl.setStyleSheet("color: #C8D8FF; font-size: 11px;")
        self._name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._name_lbl)

        self._detail_lbl = QLabel()
        self._detail_lbl.setStyleSheet("color: #00D4FF; font-size: 11px; font-family: Consolas;")
        layout.addWidget(self._detail_lbl)

        self._badge_lbl = QLabel()
        self._badge_lbl.setStyleSheet("color: #5A6690; font-size: 10px;")
        layout.addWidget(self._badge_lbl)

    def set_data(self, rank: int, node_num: int, name: str, badge: str, detail: str) -> None:
        self._node_num = node_num
        self._rank_lbl.setText(f"{rank}.")
        self._name_lbl.setText(name)
        self._detail_lbl.setText(detail)
        self._badge_lbl.setText(badge)

    def set_selected(self, selected: bool) -> None:
        # Dynamic property + stylesheet selector (see theme.py's
        # #rankItem[selected="true"]) rather than setStyleSheet() directly —
        # keeps the highlight in one place instead of duplicating the app's
        # color palette here, and survives set_data() being called again on
        # the next refresh.
        if self.property("selected") == selected:
            return
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:
        # Defense in depth: update_rows() no longer destroys rows out from
        # under an in-flight click, but a periodic-rebuild + click race is
        # a known Qt footgun class, and this guard is cheap insurance
        # against it recurring here or if this pattern gets copied elsewhere.
        if not shiboken6.isValid(self) or self._node_num is None:
            return
        self.clicked.emit(self._node_num)
        super().mousePressEvent(event)


class RankingsPanel(QWidget):
    """One ranking panel (Last Heard, Most Packets, etc.) with a flippable title."""

    node_selected = Signal(object)   # node_num — object: see _RankRow.clicked above

    def __init__(
        self,
        title_a: str,
        title_b: str,
        max_rows: int = 50,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("rankPanel")
        self._title_a = title_a
        self._title_b = title_b
        self._showing_a = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Clickable title
        self._title_btn = QPushButton(title_a)
        self._title_btn.setObjectName("rankTitle")
        self._title_btn.setFlat(True)
        self._title_btn.setToolTip("Click to toggle sort order")
        self._title_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._title_btn.clicked.connect(self._flip)
        layout.addWidget(self._title_btn)

        # Scrollable rows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch()
        scroll.setWidget(self._rows_widget)
        layout.addWidget(scroll, 1)

        self._max_rows = max_rows
        self._rows: list[_RankRow] = []
        self._selected_node_num: int | None = None

    # ------------------------------------------------------------------

    def update_rows(self, items: list[tuple]) -> None:
        """
        items: list of (node_num, display_name, badge, detail_str)

        Reuses existing row widgets rather than destroying and recreating
        them every call. This runs on a 2-second timer; the previous
        destroy-and-rebuild approach could delete the exact widget a user's
        click was in flight to, which surfaced as an "already deleted"
        RuntimeError (and, one level up the signal chain, an AttributeError
        from Qt failing to resolve a slot on an object mid-teardown). A row
        that continues representing the same rank is now the same QObject
        across refreshes, so it is never live-deleted out from under a click.
        """
        items = items[: self._max_rows]

        # Grow the pool if this refresh has more rows than we've ever shown.
        while len(self._rows) < len(items):
            row = _RankRow()
            row.clicked.connect(self.node_selected)
            insert_at = self._rows_layout.count() - 1  # before the trailing stretch
            self._rows_layout.insertWidget(insert_at, row)
            self._rows.append(row)

        for row, (rank, (node_num, name, badge, detail)) in zip(
            self._rows, enumerate(items, 1)
        ):
            row.set_data(rank, node_num, name, badge, detail)
            row.set_selected(node_num == self._selected_node_num)
            row.setVisible(True)

        # Hide rather than delete the surplus if this refresh has fewer rows
        # than a previous one — they stay pooled for the next refresh that
        # needs them, so steady-state operation allocates no new widgets.
        for row in self._rows[len(items):]:
            row.setVisible(False)

    def set_selected_node(self, node_num: int | None) -> None:
        """Highlight whichever visible row currently represents node_num (if
        any); remembered so it's reapplied on every future refresh too."""
        self._selected_node_num = node_num
        for row in self._rows:
            row.set_selected(row._node_num == node_num)

    def is_flipped(self) -> bool:
        return not self._showing_a

    def _flip(self) -> None:
        self._showing_a = not self._showing_a
        lbl = self._title_a if self._showing_a else self._title_b
        self._title_btn.setText(lbl)
