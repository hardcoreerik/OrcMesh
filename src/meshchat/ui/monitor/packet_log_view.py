"""MeshChat – PacketLogView: virtualized packet log table."""
from __future__ import annotations

import logging

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from meshchat.models.network_packet import NetworkPacket

log = logging.getLogger(__name__)

_COLUMNS = ["Time", "Dir", "Sender", "Dest", "Ch", "Port", "Hops", "SNR", "RSSI", "Src", "Summary"]
_MAX_ROWS = 20_000


class PacketLogModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Plain list, not deque(maxlen=...): a deque only supports O(1)
        # indexing near its ends — the middle is O(n) — and data() is
        # called once per visible cell on every repaint, so indexing had to
        # be O(1) here. Capacity is enforced manually in append_packet().
        self._rows: list[NetworkPacket] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row_idx = index.row()
        if row_idx >= len(self._rows):
            return None
        pkt: NetworkPacket = self._rows[row_idx]

        if role == Qt.ItemDataRole.DisplayRole:
            col = index.column()
            if col == 0:   # Time
                ts = pkt.observed_at.astimezone(tz=None)
                return ts.strftime("%H:%M:%S")
            if col == 1:   # Dir
                return "RX"
            if col == 2:   # Sender
                if pkt.sender_id:
                    return pkt.sender_id
                if pkt.sender_num:
                    return f"!{pkt.sender_num:08x}"
                return "?"
            if col == 3:   # Dest
                # Meshtastic's raw "to" field is always populated — broadcast
                # packets carry the literal broadcast address (0xFFFFFFFF),
                # they don't come through as None. Show "^all" for that case
                # instead of the raw hex, which is meaningless to a user.
                if pkt.destination_num is None or pkt.destination_num == 0xFFFFFFFF:
                    return "^all"
                return f"!{pkt.destination_num:08x}"
            if col == 4:   # Channel
                return str(pkt.channel_index) if pkt.channel_index is not None else ""
            if col == 5:   # Port
                return pkt.portnum_name or str(pkt.portnum)
            if col == 6:   # Hops
                if pkt.hops_used is not None and pkt.hop_start is not None:
                    return f"{pkt.hops_used}/{pkt.hop_start}"
                return "?"
            if col == 7:   # SNR
                return f"{pkt.rx_snr:.1f}" if pkt.rx_snr is not None else "—"
            if col == 8:   # RSSI
                return str(pkt.rx_rssi) if pkt.rx_rssi is not None else "—"
            if col == 9:   # Src
                if pkt.via_mqtt:
                    return "MQTT"
                if pkt.transport_mechanism:
                    return pkt.transport_mechanism
                return "RF"
            if col == 10:  # Summary
                if pkt.text:
                    byte_len = len(pkt.text.encode("utf-8"))
                    return f"Text message — {byte_len} UTF-8 bytes"
                return pkt.portnum_name or "Packet"
        if role == Qt.ItemDataRole.ForegroundRole:
            from PySide6.QtGui import QColor
            col = index.column()
            if col == 9 and pkt.via_mqtt:
                return QColor("#FFB800")
            if col == 7 and pkt.rx_snr is not None:
                if pkt.rx_snr >= 5:
                    return QColor("#00FF88")
                if pkt.rx_snr < -5:
                    return QColor("#FF4060")
        if role == Qt.ItemDataRole.UserRole:
            return pkt
        return None

    def append_packet(self, pkt: NetworkPacket) -> None:
        # Evict the oldest row first when at capacity, with its own
        # begin/endRemoveRows — deque(maxlen=...)'s old silent eviction on
        # append left the view thinking one more row existed than
        # rowCount() actually reported once the log filled up, since only
        # an insert notification was ever sent, never a matching removal.
        if len(self._rows) >= _MAX_ROWS:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            del self._rows[0]
            self.endRemoveRows()

        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(pkt)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self._rows.clear()
        self.endResetModel()


class PacketLogView(QWidget):
    """Virtualized live packet log."""

    # object, not int: Meshtastic node_num is a 32-bit unsigned value and
    # can exceed 0x7FFFFFFF — a Qt-typed int signal is C++ int32 and
    # silently wraps it to negative (see ARCHITECTURE.md). Not currently
    # connected to anything, but declared correctly so it's safe the
    # moment it is.
    node_inspect_requested = Signal(object)   # node_num

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Toolbar
        toolbar = QHBoxLayout()
        lbl = QLabel("PACKET LOG")
        lbl.setObjectName("sectionLabel")
        toolbar.addWidget(lbl)
        toolbar.addStretch()

        self._tail_btn = QPushButton("⇣ Tail")
        self._tail_btn.setCheckable(True)
        self._tail_btn.setChecked(True)
        self._tail_btn.setFixedWidth(60)
        self._tail_btn.setStyleSheet("font-size: 10px; padding: 2px 6px;")
        toolbar.addWidget(self._tail_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setFixedWidth(50)
        self._clear_btn.setStyleSheet("font-size: 10px; padding: 2px 6px;")
        self._clear_btn.clicked.connect(self._on_clear)
        toolbar.addWidget(self._clear_btn)

        layout.addLayout(toolbar)

        # Table
        self._model = PacketLogModel(self)
        self._table = QTableView()
        self._table.setObjectName("packetLog")
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setWordWrap(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)

        # Column widths
        widths = [68, 30, 90, 90, 28, 100, 50, 48, 48, 45, 200]
        for i, w in enumerate(widths):
            self._table.setColumnWidth(i, w)
        self._table.horizontalHeader().setSectionResizeMode(10, QHeaderView.ResizeMode.Stretch)

        self._table.verticalHeader().setDefaultSectionSize(18)
        layout.addWidget(self._table, 1)

        self._model.rowsInserted.connect(self._on_rows_inserted)

    # ------------------------------------------------------------------

    def add_packet(self, pkt: NetworkPacket) -> None:
        self._model.append_packet(pkt)

    def _on_rows_inserted(self) -> None:
        if self._tail_btn.isChecked():
            self._table.scrollToBottom()

    def _on_clear(self) -> None:
        self._model.clear()
