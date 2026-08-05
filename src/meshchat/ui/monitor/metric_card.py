"""MeshChat – MetricCard: KPI card widget for the Monitor dashboard."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class MetricCard(QFrame):
    """
    A compact KPI card displaying a title, primary value, and optional sub-value.
    """

    def __init__(
        self,
        title: str,
        value: str = "—",
        sub: str = "",
        tooltip: str = "",
        value_style: str = "default",
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("metricCard")
        if tooltip:
            self.setToolTip(tooltip)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        self._title_lbl = QLabel(title.upper())
        self._title_lbl.setObjectName("cardTitle")
        layout.addWidget(self._title_lbl)

        obj_name = {
            "green": "cardValueGreen",
            "amber": "cardValueAmber",
        }.get(value_style, "cardValue")

        self._value_lbl = QLabel(value)
        self._value_lbl.setObjectName(obj_name)
        self._value_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._value_lbl)

        self._sub_lbl = QLabel(sub)
        self._sub_lbl.setObjectName("cardSub")
        self._sub_lbl.setVisible(bool(sub))
        layout.addWidget(self._sub_lbl)

    def set_value(self, value: str, sub: str = "") -> None:
        self._value_lbl.setText(value)
        if sub:
            self._sub_lbl.setText(sub)
            self._sub_lbl.setVisible(True)
        else:
            self._sub_lbl.setVisible(False)

    def set_tooltip(self, text: str) -> None:
        self.setToolTip(text)


class MultiValueCard(QFrame):
    """
    A KPI card displaying multiple labeled sub-values (e.g. SNR / RSSI / NOISE).
    """

    def __init__(self, title: str, fields: list[str], tooltip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        if tooltip:
            self.setToolTip(tooltip)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        title_lbl = QLabel(title.upper())
        title_lbl.setObjectName("cardTitle")
        layout.addWidget(title_lbl)

        self._rows: dict[str, QLabel] = {}
        for field in fields:
            row_w = QWidget()
            from PySide6.QtWidgets import QHBoxLayout
            row = QHBoxLayout(row_w)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            key_lbl = QLabel(field)
            key_lbl.setObjectName("cardSub")
            key_lbl.setFixedWidth(48)
            val_lbl = QLabel("—")
            val_lbl.setObjectName("cardValue")
            val_lbl.setStyleSheet("font-size: 14px;")
            self._rows[field] = val_lbl
            row.addWidget(key_lbl)
            row.addWidget(val_lbl)
            row.addStretch()
            layout.addWidget(row_w)

    def set_field(self, field: str, value: str) -> None:
        if field in self._rows:
            self._rows[field].setText(value)
