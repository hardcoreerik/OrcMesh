"""MeshChat – NodeTableModel: QAbstractTableModel for the Nodes page."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.utils.time_format import relative_age

log = logging.getLogger(__name__)

_COLUMNS = [
    "Name", "Short", "Node ID", "Role", "Hardware",
    "Last Heard", "Direct", "Hops", "SNR", "RSSI",
    "Packets", "Messages", "Source",
]

_INFRA_ROLES = {"ROUTER", "ROUTER_LATE", "CLIENT_BASE", "ROUTER_CLIENT", "REPEATER"}


def _source(n: NodeSnapshot) -> str:
    if n.via_mqtt_count > n.rf_count:
        return "MQTT"
    return "RF" if n.rf_count > 0 else "?"


# Column index → how to render that column for a node. Keeping this beside
# _COLUMNS makes it obvious when the two fall out of sync (see the test that
# asserts they stay the same length).
_CELL_RENDERERS = (
    lambda n: n.long_name or n.node_id or f"!{n.node_num:08x}",
    lambda n: n.short_name or "—",
    lambda n: n.node_id or f"!{n.node_num:08x}",
    lambda n: n.role or "—",
    lambda n: n.hw_model or "—",
    lambda n: relative_age(n.last_heard),
    lambda n: "✓" if n.is_direct else "",
    lambda n: str(n.last_hops_used) if n.last_hops_used is not None else "—",
    lambda n: f"{n.last_snr:.1f}" if n.last_snr is not None else "—",
    lambda n: str(n.last_rssi) if n.last_rssi is not None else "—",
    lambda n: str(n.packet_count),
    lambda n: str(n.text_count),
    _source,
)


class NodeTableModel(QAbstractTableModel):
    """Model backing the Nodes page table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nodes: list[NodeSnapshot] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._nodes)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row >= len(self._nodes):
            return None
        n = self._nodes[row]

        if role == Qt.ItemDataRole.DisplayRole:
            col = index.column()
            if 0 <= col < len(_CELL_RENDERERS):
                return _CELL_RENDERERS[col](n)
            return None
        if role == Qt.ItemDataRole.ForegroundRole:
            col = index.column()
            if col == 6 and n.is_direct:
                return QColor("#00FF88")
            if col == 8 and n.last_snr is not None:
                if n.last_snr >= 5:
                    return QColor("#00FF88")
                if n.last_snr < -5:
                    return QColor("#FF4060")
        if role == Qt.ItemDataRole.UserRole:
            return n
        return None

    def update_nodes(self, nodes: dict[int, NodeSnapshot]) -> None:
        self.beginResetModel()
        never_heard = datetime.min.replace(tzinfo=timezone.utc)
        self._nodes = sorted(
            nodes.values(),
            key=lambda n: n.last_heard or never_heard,
            reverse=True,
        )
        self.endResetModel()

    def node_at(self, row: int) -> NodeSnapshot | None:
        if 0 <= row < len(self._nodes):
            return self._nodes[row]
        return None
