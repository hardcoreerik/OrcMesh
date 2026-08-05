"""MeshChat – NodeTableModel: QAbstractTableModel for the Nodes page."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

from meshchat.models.node_snapshot import NodeSnapshot

log = logging.getLogger(__name__)

_COLUMNS = [
    "Name", "Short", "Node ID", "Role", "Hardware",
    "Last Heard", "Direct", "Hops", "SNR", "RSSI",
    "Packets", "Messages", "Source",
]

_INFRA_ROLES = {"ROUTER", "ROUTER_LATE", "CLIENT_BASE", "ROUTER_CLIENT", "REPEATER"}


def _age(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    delta = (datetime.now(timezone.utc) - ts).total_seconds()
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


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
            if col == 0:  return n.long_name or n.node_id or f"!{n.node_num:08x}"
            if col == 1:  return n.short_name or "—"
            if col == 2:  return n.node_id or f"!{n.node_num:08x}"
            if col == 3:  return n.role or "—"
            if col == 4:  return n.hw_model or "—"
            if col == 5:  return _age(n.last_heard)
            if col == 6:  return "✓" if n.is_direct else ""
            if col == 7:  return str(n.last_hops_used) if n.last_hops_used is not None else "—"
            if col == 8:  return f"{n.last_snr:.1f}" if n.last_snr is not None else "—"
            if col == 9:  return str(n.last_rssi) if n.last_rssi is not None else "—"
            if col == 10: return str(n.packet_count)
            if col == 11: return str(n.text_count)
            if col == 12:
                if n.via_mqtt_count > n.rf_count:
                    return "MQTT"
                if n.rf_count > 0:
                    return "RF"
                return "?"
        if role == Qt.ItemDataRole.ForegroundRole:
            from PySide6.QtGui import QColor
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
        self._nodes = sorted(nodes.values(), key=lambda n: n.last_heard or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        self.endResetModel()

    def node_at(self, row: int) -> NodeSnapshot | None:
        if 0 <= row < len(self._nodes):
            return self._nodes[row]
        return None
