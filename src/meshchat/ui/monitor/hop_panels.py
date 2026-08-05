"""MeshChat – HopPanels: hop counts and distribution analytics."""
from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from meshchat.analytics.hop_metrics import hop_efficiency_bucket


class HopCountBar(QWidget):
    """One horizontal bar row: label + bar + count."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._lbl = QLabel(label)
        self._lbl.setFixedWidth(60)
        self._lbl.setStyleSheet("color: #7A8FBF; font-size: 10px; font-family: Consolas;")
        layout.addWidget(self._lbl)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        self._bar.setStyleSheet("""
            QProgressBar { border: none; border-radius: 4px; background: #0D1530; }
            QProgressBar::chunk { background: #00D4FF; border-radius: 4px; }
        """)
        layout.addWidget(self._bar, 1)

        self._count = QLabel("0")
        self._count.setFixedWidth(36)
        self._count.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._count.setStyleSheet("color: #C8D8FF; font-size: 10px; font-family: Consolas;")
        layout.addWidget(self._count)

    def update(self, pct: int, count: int) -> None:
        self._bar.setValue(max(0, min(100, pct)))
        self._count.setText(str(count))


class HopCountsPanel(QFrame):
    """HOPS USED distribution panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        title = QLabel("HOPS USED")
        title.setObjectName("cardTitle")
        layout.addWidget(title)

        self._bars: dict[str, HopCountBar] = {}
        for label in ["0", "1", "2", "3", "4", "5+", "?"]:
            bar = HopCountBar(label)
            self._bars[label] = bar
            layout.addWidget(bar)

        layout.addStretch()

    def update_data(self, counts: dict[str, int]) -> None:
        total = sum(counts.values()) or 1
        for key, bar in self._bars.items():
            n = counts.get(key, 0)
            pct = int(n * 100 / total)
            bar.update(pct, n)


class PercentHopsPanel(QFrame):
    """Percent of hops used distribution."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        title = QLabel("% HOPS USED")
        title.setObjectName("cardTitle")
        title.setToolTip(
            "100% used means hop_limit reached zero by the time the packet arrived.\n"
            "It does not prove the packet's complete route."
        )
        layout.addWidget(title)

        self._bars: dict[str, HopCountBar] = {}
        for bucket in ["0%", "1–24%", "25–49%", "50–74%", "75–99%", "100%", "Unknown"]:
            bar = HopCountBar(bucket)
            self._bars[bucket] = bar
            layout.addWidget(bar)

        layout.addStretch()

    def update_packets(self, packets) -> None:
        """packets: iterable of NetworkPacket."""
        counts: dict[str, int] = defaultdict(int)
        for pkt in packets:
            if pkt.hops_used is not None and pkt.hop_start and pkt.hop_start > 0:
                pct = pkt.hops_used / pkt.hop_start
            else:
                pct = None
            bucket = hop_efficiency_bucket(pct)
            counts[bucket] += 1
        total = sum(counts.values()) or 1
        for key, bar in self._bars.items():
            n = counts.get(key, 0)
            bar.update(int(n * 100 / total), n)


class HopPanels(QWidget):
    """Container for hop analytics panels stacked vertically."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.hop_counts = HopCountsPanel()
        layout.addWidget(self.hop_counts)

        self.pct_hops = PercentHopsPanel()
        layout.addWidget(self.pct_hops)

        layout.addStretch()

    def update_data(self, packets) -> None:
        """Update both panels from a list of NetworkPacket."""
        hops_used_counts: dict[str, int] = defaultdict(int)
        for pkt in packets:
            if pkt.hops_used is None:
                hops_used_counts["?"] += 1
            elif pkt.hops_used >= 5:
                hops_used_counts["5+"] += 1
            else:
                hops_used_counts[str(pkt.hops_used)] += 1
        self.hop_counts.update_data(dict(hops_used_counts))
        self.pct_hops.update_packets(packets)
