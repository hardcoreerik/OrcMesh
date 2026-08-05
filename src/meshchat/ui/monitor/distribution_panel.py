"""MeshChat – DistributionPanel: Packet Breakdown / Roles / Hardware panels."""
from __future__ import annotations

from collections import Counter, defaultdict

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)


class _DistRow(QWidget):
    clicked = Signal(str)  # category

    def __init__(self, category: str, color: str = "#00D4FF", parent=None):
        super().__init__(parent)
        self._category = category
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 9px;")
        dot.setFixedWidth(12)
        layout.addWidget(dot)

        self._name_lbl = QLabel(category)
        self._name_lbl.setStyleSheet("color: #C8D8FF; font-size: 10px;")
        layout.addWidget(self._name_lbl, 1)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._bar.setStyleSheet(f"""
            QProgressBar {{ border: none; border-radius: 3px; background: #0D1530; }}
            QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}
        """)
        layout.addWidget(self._bar, 2)

        self._count_lbl = QLabel("0")
        self._count_lbl.setFixedWidth(36)
        self._count_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._count_lbl.setStyleSheet("color: #7A8FBF; font-size: 10px; font-family: Consolas;")
        layout.addWidget(self._count_lbl)

    def set_values(self, count: int, pct: int) -> None:
        self._bar.setValue(pct)
        self._count_lbl.setText(str(count))
        self._count_lbl.setToolTip(f"{pct}%")

    def mousePressEvent(self, event) -> None:
        # Rows here are destroyed and rebuilt on every refresh. A click racing
        # that rebuild can land on a row whose C++ side is already gone — this
        # exact crash was hit via user report in RankingsPanel, which now
        # pools its rows instead (see rankings_panel.py) as the real fix.
        # This panel refreshes less aggressively, so a defensive guard is
        # proportionate here rather than the same pooling rewrite.
        if not shiboken6.isValid(self):
            return
        self.clicked.emit(self._category)
        super().mousePressEvent(event)


_PORTNUM_LABELS = {
    1:   ("Text",             "#00D4FF"),
    3:   ("Position",         "#00FF88"),
    4:   ("NodeInfo",         "#8B5CF6"),
    5:   ("Routing",          "#4F9EFF"),
    67:  ("Telemetry",        "#FFB800"),
    70:  ("Traceroute",       "#FF8C00"),
    71:  ("NeighborInfo",     "#FF69B4"),
    73:  ("MapReport",        "#20B2AA"),
    256: ("PrivateApp",       "#5A6690"),
}

_ROLE_COLORS = {
    "ROUTER":         "#00D4FF",
    "CLIENT":         "#8B5CF6",
    "TRACKER":        "#00FF88",
    "SENSOR":         "#FFB800",
    "ROUTER_LATE":    "#4F9EFF",
    "CLIENT_BASE":    "#9B5CF6",
    "CLIENT_MUTE":    "#7B7CF6",
    "CLIENT_HIDDEN":  "#5A4CF6",
    "UNKNOWN":        "#5A6690",
}


class PacketBreakdownPanel(QFrame):
    """Packet type distribution."""

    category_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        title = QLabel("PACKET BREAKDOWN")
        title.setObjectName("cardTitle")
        layout.addWidget(title)

        self._rows: dict[str, _DistRow] = {}
        ordered = list(_PORTNUM_LABELS.values()) + [
            ("Other Known", "#3A4870"),
            ("Unknown/Encrypted", "#FF4060"),
        ]
        for label, color in ordered:
            row = _DistRow(label, color)
            row.clicked.connect(self.category_clicked)
            self._rows[label] = row
            layout.addWidget(row)

        layout.addStretch()

    def update_packets(self, packets) -> None:
        counts: dict[str, int] = defaultdict(int)
        for pkt in packets:
            label, _ = _PORTNUM_LABELS.get(pkt.portnum, ("Other Known", ""))
            counts[label] += 1
        total = sum(counts.values()) or 1
        for label, row in self._rows.items():
            n = counts.get(label, 0)
            row.set_values(n, int(n * 100 / total))


class RolesPanel(QFrame):
    """Node role distribution."""

    role_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        title = QLabel("ROLES")
        title.setObjectName("cardTitle")
        title.setToolTip(
            "Based on most recently received NodeInfo for each node.\n"
            "Not live radio configuration."
        )
        layout.addWidget(title)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(3)
        layout.addLayout(self._rows_layout)
        layout.addStretch()

        self._rows: dict[str, _DistRow] = {}

    def update_nodes(self, nodes: list) -> None:
        """nodes: iterable of NodeSnapshot."""
        counts: Counter = Counter()
        for n in nodes:
            role = (n.role or "UNKNOWN").upper()
            counts[role] += 1

        # Remove old rows
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._rows.clear()

        total = sum(counts.values()) or 1
        for role, count in counts.most_common(10):
            color = _ROLE_COLORS.get(role, "#5A6690")
            row = _DistRow(role, color)
            row.clicked.connect(self.role_clicked)
            row.set_values(count, int(count * 100 / total))
            self._rows[role] = row
            self._rows_layout.addWidget(row)


class HardwarePanel(QFrame):
    """Hardware model distribution."""

    hw_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        title = QLabel("HARDWARE")
        title.setObjectName("cardTitle")
        layout.addWidget(title)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(3)
        layout.addLayout(self._rows_layout)
        layout.addStretch()

        self._rows: dict[str, _DistRow] = {}

    @staticmethod
    def _friendly_hw(hw_model: str | None) -> str:
        if not hw_model:
            return "Unknown"
        # Simple title-case transformation of underscore names
        parts = hw_model.replace("_", " ").title()
        return parts

    def update_nodes(self, nodes: list) -> None:
        counts: Counter = Counter()
        for n in nodes:
            hw = self._friendly_hw(n.hw_model)
            counts[hw] += 1

        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._rows.clear()

        total = sum(counts.values()) or 1
        colors = ["#4F9EFF", "#8B5CF6", "#00FF88", "#FFB800", "#FF8C00",
                  "#FF69B4", "#20B2AA", "#00D4FF", "#5A6690", "#C8D8FF"]
        for i, (hw, count) in enumerate(counts.most_common(10)):
            color = colors[i % len(colors)]
            row = _DistRow(hw, color)
            row.clicked.connect(self.hw_clicked)
            row.set_values(count, int(count * 100 / total))
            self._rows[hw] = row
            self._rows_layout.addWidget(row)


class ChannelsPanel(QFrame):
    """Per-channel traffic monitor — packet counts on each of the radio's
    logical Meshtastic channels (a channel is a crypto-scoped conversation,
    not an RF frequency; see the header's radio-config label for the
    physical LoRa region/frequency instead)."""

    channel_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        title = QLabel("CHANNEL MONITOR")
        title.setObjectName("cardTitle")
        title.setToolTip(
            "Packet traffic per logical Meshtastic channel (0-7).\n"
            "Named channels come from the connected radio's channel list."
        )
        layout.addWidget(title)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(3)
        layout.addLayout(self._rows_layout)
        layout.addStretch()

        self._rows: dict[int, _DistRow] = {}
        _colors = ["#00D4FF", "#8B5CF6", "#00FF88", "#FFB800", "#FF8C00",
                   "#FF69B4", "#20B2AA", "#4F9EFF"]
        self._colors = _colors

    def update_data(self, packets, channel_names: dict[int, str]) -> None:
        counts: Counter = Counter()
        for pkt in packets:
            idx = pkt.channel_index if pkt.channel_index is not None else 0
            counts[idx] += 1

        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._rows.clear()

        total = sum(counts.values()) or 1
        for idx, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            name = channel_names.get(idx, f"Ch {idx}")
            label = f"{idx}: {name}"
            color = self._colors[idx % len(self._colors)]
            row = _DistRow(label, color)
            row.clicked.connect(lambda _c, i=idx: self.channel_clicked.emit(str(i)))
            row.set_values(count, int(count * 100 / total))
            self._rows[idx] = row
            self._rows_layout.addWidget(row)


class DistributionPanel(QWidget):
    """Container wrapping Breakdown, Channels, Roles, and Hardware panels."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.breakdown = PacketBreakdownPanel()
        layout.addWidget(self.breakdown)

        self.channels = ChannelsPanel()
        layout.addWidget(self.channels)

        self.roles = RolesPanel()
        layout.addWidget(self.roles)

        self.hardware = HardwarePanel()
        layout.addWidget(self.hardware)

        layout.addStretch()
