"""MeshChat – FilterBar: age/source/packet-type filter controls."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)


class FilterBar(QWidget):
    """Horizontal filter bar for the Monitor page."""

    filter_changed = Signal(dict)   # {"age_s": int | None, "source": str, "portnum": str}

    _AGE_OPTIONS = [
        ("5 min",  300),
        ("15 min", 900),
        ("1 hour", 3600),
        ("6 hours", 21600),
        ("24 hours", 86400),
        ("7 days",  604800),
        ("All",   None),
    ]
    _SRC_OPTIONS = ["All observed", "RF only", "MQTT-path", "Unknown transport"]
    _PKT_OPTIONS = [
        "All",
        "Text",
        "Position",
        "NodeInfo",
        "Telemetry",
        "Routing",
        "Traceroute",
        "NeighborInfo",
        "PrivateApp",
        "Other",
        "Unknown/Encrypted",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        layout.addWidget(QLabel("Age:"))
        self._age = QComboBox()
        self._age.addItems([label for label, _ in self._AGE_OPTIONS])
        self._age.setCurrentIndex(2)   # 1 hour default
        self._age.setFixedWidth(80)
        self._age.currentIndexChanged.connect(self._emit)
        layout.addWidget(self._age)

        layout.addWidget(QLabel("Source:"))
        self._src = QComboBox()
        self._src.addItems(self._SRC_OPTIONS)
        self._src.setFixedWidth(130)
        self._src.currentIndexChanged.connect(self._emit)
        layout.addWidget(self._src)

        layout.addWidget(QLabel("Type:"))
        self._pkt = QComboBox()
        self._pkt.addItems(self._PKT_OPTIONS)
        self._pkt.setFixedWidth(120)
        self._pkt.currentIndexChanged.connect(self._emit)
        layout.addWidget(self._pkt)

        layout.addStretch()

        reset_btn = QPushButton("Reset")
        reset_btn.setFixedWidth(60)
        reset_btn.setStyleSheet("font-size: 10px; padding: 2px 6px;")
        reset_btn.clicked.connect(self._reset)
        layout.addWidget(reset_btn)

    def current_filter(self) -> dict:
        age_s = self._AGE_OPTIONS[self._age.currentIndex()][1]
        return {
            "age_s": age_s,
            "source": self._src.currentText(),
            "portnum": self._pkt.currentText(),
        }

    def _emit(self) -> None:
        self.filter_changed.emit(self.current_filter())

    def _reset(self) -> None:
        # Each setCurrentIndex() below fires currentIndexChanged -> _emit()
        # on its own whenever it actually changes that combo's value, so a
        # naive reset can emit filter_changed up to 3 extra times before the
        # explicit call below — each one triggering a full rankings rebuild
        # in MonitorPage. Block signals during the resets and emit once.
        for combo in (self._age, self._src, self._pkt):
            combo.blockSignals(True)
        try:
            self._age.setCurrentIndex(2)
            self._src.setCurrentIndex(0)
            self._pkt.setCurrentIndex(0)
        finally:
            for combo in (self._age, self._src, self._pkt):
                combo.blockSignals(False)
        self._emit()
