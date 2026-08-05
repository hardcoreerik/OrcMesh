"""MeshChat – RankingsPanel: Last Heard / Most Packets / Nearby / Big Signal / Messages Sent."""
from __future__ import annotations

import logging

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
    clicked = Signal(int)  # node_num

    def __init__(self, rank: int, node_num: int, name: str, badge: str, detail: str, parent=None):
        super().__init__(parent)
        self._node_num = node_num
        self.setObjectName("rankItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        rank_lbl = QLabel(f"{rank}.")
        rank_lbl.setStyleSheet("color: #3A4870; font-size: 10px; min-width: 16px;")
        layout.addWidget(rank_lbl)

        name_lbl = QLabel(name)
        name_lbl.setStyleSheet("color: #C8D8FF; font-size: 11px;")
        name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(name_lbl)

        detail_lbl = QLabel(detail)
        detail_lbl.setStyleSheet("color: #00D4FF; font-size: 11px; font-family: Consolas;")
        layout.addWidget(detail_lbl)

        badge_lbl = QLabel(badge)
        badge_lbl.setStyleSheet("color: #5A6690; font-size: 10px;")
        layout.addWidget(badge_lbl)

    def mousePressEvent(self, event) -> None:
        self.clicked.emit(self._node_num)
        super().mousePressEvent(event)


class RankingsPanel(QWidget):
    """One ranking panel (Last Heard, Most Packets, etc.) with a flippable title."""

    node_selected = Signal(int)   # node_num

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

    # ------------------------------------------------------------------

    def update_rows(self, items: list[tuple]) -> None:
        """
        items: list of (node_num, display_name, badge, detail_str)
        """
        # Clear existing rows
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        for rank, (node_num, name, badge, detail) in enumerate(items[: self._max_rows], 1):
            row = _RankRow(rank, node_num, name, badge, detail)
            row.clicked.connect(self.node_selected)
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)

    def is_flipped(self) -> bool:
        return not self._showing_a

    def _flip(self) -> None:
        self._showing_a = not self._showing_a
        lbl = self._title_a if self._showing_a else self._title_b
        self._title_btn.setText(lbl)
