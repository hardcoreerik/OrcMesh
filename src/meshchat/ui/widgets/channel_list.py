"""MeshChat – ChannelList sidebar widget."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

_ROLE_NODE_NUM = Qt.ItemDataRole.UserRole


class ChannelList(QWidget):
    """Sidebar showing active channels plus a Direct Messages node picker."""

    channel_selected = Signal(int)     # channel_index
    dm_target_selected = Signal(int, str)   # node_num, display_name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("channelList")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        hdr = QLabel("CHANNELS")
        hdr.setObjectName("sectionLabel")
        hdr.setContentsMargins(12, 8, 0, 4)
        layout.addWidget(hdr)

        self._list = QListWidget()
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        self._list.currentRowChanged.connect(self._on_channel_row_changed)
        layout.addWidget(self._list)

        dm_hdr = QLabel("DIRECT MESSAGES")
        dm_hdr.setObjectName("sectionLabel")
        dm_hdr.setContentsMargins(12, 10, 0, 4)
        layout.addWidget(dm_hdr)

        self._dm_list = QListWidget()
        self._dm_list.setFrameShape(QListWidget.Shape.NoFrame)
        self._dm_list.currentRowChanged.connect(self._on_dm_row_changed)
        layout.addWidget(self._dm_list, 1)

        self._channels: list = []
        self._dm_nodes: list = []       # list[NodeSnapshot], excluding local node
        self._local_node_num: int | None = None

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def set_channels(self, channels: list) -> None:
        self._channels = channels
        self._list.clear()
        for ch in channels:
            badge = "★" if ch.role == "PRIMARY" else "·"
            item = QListWidgetItem(f"{badge}  {ch.name}")
            item.setToolTip(f"Channel {ch.index}  —  {ch.role}")
            item.setData(_ROLE_NODE_NUM, ch.index)
            self._list.addItem(item)
        if channels:
            self._list.setCurrentRow(0)

    def clear_channels(self) -> None:
        self._channels = []
        self._list.clear()

    def current_channel_index(self) -> int | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return item.data(_ROLE_NODE_NUM)

    def _on_channel_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._channels):
            return
        self._dm_list.setCurrentRow(-1)
        self.channel_selected.emit(self._channels[row].index)

    # ------------------------------------------------------------------
    # Direct messages
    # ------------------------------------------------------------------

    def set_local_node(self, node_num: int | None) -> None:
        self._local_node_num = node_num

    def set_dm_nodes(self, nodes: list) -> None:
        """Populate the DM list from known NodeSnapshots (any node we can reach
        through the mesh — not just direct-RF neighbors — can be messaged)."""
        selected_num = self.current_dm_target()
        self._dm_nodes = sorted(
            (n for n in nodes if n.node_num != self._local_node_num),
            key=lambda n: n.display_name.lower(),
        )
        self._dm_list.blockSignals(True)
        self._dm_list.clear()
        restore_row = -1
        for i, n in enumerate(self._dm_nodes):
            item = QListWidgetItem(f"@  {n.display_name}")
            item.setToolTip(f"Node !{n.node_num:08x}")
            item.setData(_ROLE_NODE_NUM, n.node_num)
            self._dm_list.addItem(item)
            if n.node_num == selected_num:
                restore_row = i
        self._dm_list.blockSignals(False)
        if restore_row >= 0:
            self._dm_list.setCurrentRow(restore_row)

    def current_dm_target(self) -> int | None:
        item = self._dm_list.currentItem()
        if item is None:
            return None
        return item.data(_ROLE_NODE_NUM)

    def _on_dm_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._dm_nodes):
            return
        self._list.setCurrentRow(-1)
        node = self._dm_nodes[row]
        self.dm_target_selected.emit(node.node_num, node.display_name)
