"""MeshChat – NodesPage: full sortable node table with inspector."""
from __future__ import annotations

import logging

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from meshchat.models.node_snapshot import NodeSnapshot
from meshchat.ui.monitor.node_inspector import NodeInspector
from meshchat.ui.nodes.node_table_model import NodeTableModel

log = logging.getLogger(__name__)


class NodesPage(QWidget):
    """Full node table + inspector side panel."""

    node_selected = Signal(int)          # node_num
    # Context-menu actions, handled by MainWindow (which owns the controller)
    message_requested = Signal(int, str)  # node_num, display_name
    show_on_map_requested = Signal(int)
    position_requested = Signal(int)
    telemetry_requested = Signal(int)
    traceroute_requested = Signal(int)
    favorite_requested = Signal(int, bool)
    remove_node_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar
        toolbar = QWidget()
        toolbar.setStyleSheet("background: #0D1530; border-bottom: 1px solid #1A2448;")
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 6, 8, 6)
        tb_layout.setSpacing(8)

        lbl = QLabel("NODES")
        lbl.setObjectName("monitorTitle")
        tb_layout.addWidget(lbl)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by name, ID, or node number…")
        self._search.setFixedWidth(280)
        self._search.textChanged.connect(self._on_search)
        tb_layout.addWidget(self._search)

        tb_layout.addStretch()

        self._count_lbl = QLabel("0 nodes")
        self._count_lbl.setStyleSheet("color: #5A6690; font-size: 11px;")
        tb_layout.addWidget(self._count_lbl)

        layout.addWidget(toolbar)

        # Main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        # Table
        self._model = NodeTableModel()
        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)  # all columns

        self._table = QTableView()
        self._table.setObjectName("packetLog")
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setWordWrap(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)
        self._table.horizontalHeader().setSortIndicatorShown(True)
        self._table.verticalHeader().setDefaultSectionSize(20)

        widths = [160, 55, 95, 90, 120, 65, 50, 45, 55, 55, 60, 60, 55]
        for i, w in enumerate(widths):
            self._table.setColumnWidth(i, w)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        self._table.selectionModel().currentRowChanged.connect(self._on_row_changed)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        splitter.addWidget(self._table)

        # Inspector
        self._inspector = NodeInspector()
        self._inspector.setMinimumWidth(260)
        self._inspector.setMaximumWidth(340)
        self._inspector.show_on_map.connect(self.show_on_map_requested)
        splitter.addWidget(self._inspector)
        splitter.setSizes([700, 300])

    # ------------------------------------------------------------------

    def update_nodes(self, nodes: dict[int, NodeSnapshot]) -> None:
        self._model.update_nodes(nodes)
        self._count_lbl.setText(f"{self._proxy.rowCount()} node(s)")

    def select_node(self, node_num: int) -> None:
        """Select and scroll to a node's row, e.g. after a click elsewhere
        (map pin, ranking panel) asks this page to follow along."""
        row = self._model.row_of(node_num)
        if row is None:
            return
        proxy_index = self._proxy.mapFromSource(self._model.index(row, 0))
        if not proxy_index.isValid():
            return
        self._table.setCurrentIndex(proxy_index)
        self._table.scrollTo(proxy_index)

    def _on_search(self, text: str) -> None:
        self._proxy.setFilterFixedString(text)
        self._count_lbl.setText(f"{self._proxy.rowCount()} node(s)")

    def _on_row_changed(self, current, previous) -> None:
        src_idx = self._proxy.mapToSource(current)
        node = self._model.node_at(src_idx.row())
        if node:
            self._inspector.show_node(node)
            self.node_selected.emit(node.node_num)

    def _on_double_click(self, index) -> None:
        self._on_row_changed(index, None)

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _node_at_point(self, pos):
        index = self._table.indexAt(pos)
        if not index.isValid():
            return None
        return self._model.node_at(self._proxy.mapToSource(index).row())

    def _on_context_menu(self, pos) -> None:
        node = self._node_at_point(pos)
        if node is None:
            return

        # Select the row the user right-clicked so the inspector follows along
        index = self._table.indexAt(pos)
        self._table.setCurrentIndex(index)

        name = node.display_name
        menu = QMenu(self)

        act_msg = QAction(f"Message {name}…", self)
        act_msg.triggered.connect(lambda: self.message_requested.emit(node.node_num, name))
        menu.addAction(act_msg)

        act_map = QAction("Show on Map", self)
        act_map.triggered.connect(lambda: self.show_on_map_requested.emit(node.node_num))
        menu.addAction(act_map)

        menu.addSeparator()

        act_pos = QAction("Request Position", self)
        act_pos.triggered.connect(lambda: self.position_requested.emit(node.node_num))
        menu.addAction(act_pos)

        act_tel = QAction("Request Telemetry", self)
        act_tel.triggered.connect(lambda: self.telemetry_requested.emit(node.node_num))
        menu.addAction(act_tel)

        act_trace = QAction("Traceroute", self)
        act_trace.setToolTip("Ask the mesh to report the hop-by-hop path to this node")
        act_trace.triggered.connect(lambda: self.traceroute_requested.emit(node.node_num))
        menu.addAction(act_trace)

        menu.addSeparator()

        act_fav = QAction("Add to Favorites (on radio)", self)
        act_fav.triggered.connect(lambda: self.favorite_requested.emit(node.node_num, True))
        menu.addAction(act_fav)

        act_unfav = QAction("Remove from Favorites (on radio)", self)
        act_unfav.triggered.connect(lambda: self.favorite_requested.emit(node.node_num, False))
        menu.addAction(act_unfav)

        menu.addSeparator()

        copy_menu = menu.addMenu("Copy")
        for label, value in (
            ("Node Name", name),
            ("Node ID", node.node_id or f"!{node.node_num:08x}"),
            ("Node Number", str(node.node_num)),
        ):
            act = QAction(label, self)
            act.triggered.connect(lambda _c=False, v=value: QGuiApplication.clipboard().setText(v))
            copy_menu.addAction(act)

        menu.addSeparator()

        act_remove = QAction("Remove Node from Radio…", self)
        act_remove.triggered.connect(lambda: self._confirm_remove(node.node_num, name))
        menu.addAction(act_remove)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _confirm_remove(self, node_num: int, name: str) -> None:
        # Removing writes to the radio's own NodeDB — confirm before doing it.
        reply = QMessageBox.question(
            self,
            "Remove Node",
            f"Remove “{name}” from the connected radio's node database?\n\n"
            "The node will reappear if it transmits again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.remove_node_requested.emit(node_num)
