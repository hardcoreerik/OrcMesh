"""MeshChat – ChatView: the main chat interface."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from meshchat.controllers.meshtastic_controller import (
    MAX_MESSAGE_BYTES,
    ChatMessage,
    MessageStatus,
    normalize_outgoing_text,
)
from meshchat.ui.widgets.message_bubble import MessageBubble

log = logging.getLogger(__name__)

# Mirrors the controller's enforced limit — imported rather than restated so
# the counter shown to the user can never disagree with what is enforced.
_BYTE_LIMIT = MAX_MESSAGE_BYTES


class ComposerWidget(QWidget):
    """Multi-line text composer with byte counter and Send button."""

    send_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(4)

        # Text input + send button row
        row = QHBoxLayout()
        self._input = QTextEdit()
        self._input.setObjectName("composerInput")
        self._input.setPlaceholderText("Type a message…  (Enter to send, Shift+Enter for new line)")
        self._input.setFixedHeight(72)
        self._input.textChanged.connect(self._on_text_changed)
        self._input.installEventFilter(self)
        row.addWidget(self._input)

        self._send_btn = QPushButton("Send")
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.setFixedSize(70, 64)
        self._send_btn.setEnabled(False)
        self._send_btn.clicked.connect(self._do_send)
        row.addWidget(self._send_btn)
        layout.addLayout(row)

        # Byte count
        self._byte_label = QLabel(f"0 / {_BYTE_LIMIT} bytes")
        self._byte_label.setObjectName("byteCount")
        self._byte_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._byte_label)

    def eventFilter(self, obj, event) -> bool:
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            key_event: QKeyEvent = event
            if key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if not (key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                    self._do_send()
                    return True
        return super().eventFilter(obj, event)

    def _on_text_changed(self) -> None:
        # Count the normalised form — the same string the radio will receive.
        # Counting raw editor text made a trailing CRLF read as 2 extra bytes
        # and greyed out Send on messages that were actually within the limit.
        text = normalize_outgoing_text(self._input.toPlainText())
        byte_len = len(text.encode("utf-8"))
        lbl = f"{byte_len} / {_BYTE_LIMIT} bytes"
        if byte_len > _BYTE_LIMIT:
            self._byte_label.setObjectName("byteCountOver")
            self._byte_label.setText(f"⚠ {lbl}")
        elif byte_len > _BYTE_LIMIT * 0.85:
            self._byte_label.setObjectName("byteCountWarn")
            self._byte_label.setText(lbl)
        else:
            self._byte_label.setObjectName("byteCount")
            self._byte_label.setText(lbl)
        self._byte_label.style().unpolish(self._byte_label)
        self._byte_label.style().polish(self._byte_label)
        has_text = bool(text) and byte_len <= _BYTE_LIMIT
        self._send_btn.setEnabled(has_text and self.isEnabled())

    def _do_send(self) -> None:
        text = normalize_outgoing_text(self._input.toPlainText())
        if not text:
            return
        byte_len = len(text.encode("utf-8"))
        if byte_len > _BYTE_LIMIT:
            return
        self.send_requested.emit(text)
        self._input.clear()

    def set_enabled_for_send(self, enabled: bool) -> None:
        self.setEnabled(enabled)
        if not enabled:
            self._send_btn.setEnabled(False)


class ChatView(QWidget):
    """
    Main chat page: channel list sidebar + scrollable bubble area + composer.
    """

    send_requested = Signal(str, int)          # text, channel_index (broadcast)
    send_direct_requested = Signal(str, int)   # text, destination_num (direct message)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatArea")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Scrollable bubble area
        self._scroll = QScrollArea()
        self._scroll.setObjectName("chatScrollArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._bubble_container = QWidget()
        self._bubble_layout = QVBoxLayout(self._bubble_container)
        self._bubble_layout.setContentsMargins(4, 8, 4, 8)
        self._bubble_layout.setSpacing(4)
        self._bubble_layout.addStretch()

        self._scroll.setWidget(self._bubble_container)
        layout.addWidget(self._scroll, 1)

        # Empty-state label (shown when no messages)
        self._empty_label = QLabel(
            "Connect to a Meshtastic radio to begin chatting.\n\n"
            "Select a channel from the sidebar, then type a message."
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: #3A4870; font-size: 14px; padding: 40px;")
        self._bubble_layout.insertWidget(0, self._empty_label)

        # Composer
        self._composer = ComposerWidget()
        self._composer.send_requested.connect(self._on_send)
        layout.addWidget(self._composer)

        # State
        # _current_key is ("channel", channel_index) or ("dm", node_num)
        self._current_key: tuple | None = None
        self._current_dm_name: str = ""
        self._bubbles: dict[str, MessageBubble] = {}        # local_id → bubble
        self._messages_by_key: dict[tuple, list[ChatMessage]] = {}
        self._at_bottom = True

        # Auto-scroll check
        vsb = self._scroll.verticalScrollBar()
        vsb.valueChanged.connect(self._on_scroll_changed)
        vsb.rangeChanged.connect(self._on_range_changed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_channel(self, channel_index: int) -> None:
        """Switch displayed conversation to the given broadcast channel."""
        self._current_key = ("channel", channel_index)
        self._rebuild_bubbles()

    def set_dm_target(self, node_num: int, name: str) -> None:
        """Switch displayed conversation to a direct-message thread with a node."""
        self._current_key = ("dm", node_num)
        self._current_dm_name = name
        self._rebuild_bubbles()

    @staticmethod
    def _key_for(msg: ChatMessage) -> tuple:
        if msg.destination_num is not None:
            return ("dm", msg.destination_num)
        return ("channel", msg.channel_index)

    def add_message(self, msg: ChatMessage) -> None:
        """Append an incoming or outgoing message."""
        key = self._key_for(msg)
        self._messages_by_key.setdefault(key, []).append(msg)

        if key == self._current_key:
            self._append_bubble(msg)

    def load_history(self, messages: list[ChatMessage]) -> None:
        """Bulk-load persisted messages (e.g. on startup) without touching the live view."""
        for msg in messages:
            self._messages_by_key.setdefault(self._key_for(msg), []).append(msg)
        if self._current_key is not None:
            self._rebuild_bubbles()

    def update_message_status(self, packet_id: int, status: MessageStatus) -> None:
        """Update delivery status on an outbound message bubble."""
        for local_id, bubble in self._bubbles.items():
            if bubble._msg.packet_id == packet_id:
                bubble.update_status(status)
                break

    def set_send_enabled(self, enabled: bool) -> None:
        self._composer.set_enabled_for_send(enabled)

    def clear_for_disconnect(self) -> None:
        self._composer.set_enabled_for_send(False)

    def set_no_channels_message(self) -> None:
        self._clear_bubbles()
        self._empty_label.setText(
            "The radio connected successfully, but no active Meshtastic channels were found.\n\n"
            "Configure a channel on the radio and reconnect."
        )
        self._empty_label.setVisible(True)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _rebuild_bubbles(self) -> None:
        self._clear_bubbles()
        key = self._current_key
        msgs = self._messages_by_key.get(key, []) if key is not None else []
        if msgs:
            self._empty_label.setVisible(False)
            for m in msgs:
                self._append_bubble(m, scroll=False)
            QTimer.singleShot(50, self._scroll_to_bottom)
        else:
            if self._current_key and self._current_key[0] == "dm":
                self._empty_label.setText(
                    f"No messages yet with {self._current_dm_name}.\n\n"
                    "Type a message to start a direct conversation."
                )
            else:
                self._empty_label.setText(
                    "Connect to a Meshtastic radio to begin chatting.\n\n"
                    "Select a channel from the sidebar, then type a message."
                )
            self._empty_label.setVisible(True)

    def _append_bubble(self, msg: ChatMessage, scroll: bool = True) -> None:
        self._empty_label.setVisible(False)
        bubble = MessageBubble(msg)
        self._bubbles[msg.local_id] = bubble
        # Insert before the trailing stretch
        insert_pos = self._bubble_layout.count() - 1
        self._bubble_layout.insertWidget(insert_pos, bubble)
        if scroll and self._at_bottom:
            QTimer.singleShot(30, self._scroll_to_bottom)

    def _clear_bubbles(self) -> None:
        self._bubbles.clear()
        # Remove all except the stretch and empty label
        while self._bubble_layout.count() > 2:
            item = self._bubble_layout.takeAt(1)
            if item and item.widget():
                item.widget().deleteLater()

    def _scroll_to_bottom(self) -> None:
        vsb = self._scroll.verticalScrollBar()
        vsb.setValue(vsb.maximum())

    def _on_scroll_changed(self, value: int) -> None:
        vsb = self._scroll.verticalScrollBar()
        self._at_bottom = (value >= vsb.maximum() - 30)

    def _on_range_changed(self, _min: int, _max: int) -> None:
        if self._at_bottom:
            self._scroll.verticalScrollBar().setValue(_max)

    def _on_send(self, text: str) -> None:
        if self._current_key is None:
            return
        kind, value = self._current_key
        if kind == "dm":
            self.send_direct_requested.emit(text, value)
        else:
            self.send_requested.emit(text, value)
