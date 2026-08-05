"""MeshChat – MessageBubble: renders a single chat message."""
from __future__ import annotations


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from meshchat.controllers.meshtastic_controller import (
    ChatMessage,
    MessageDirection,
    MessageStatus,
)

_STATUS_TEXT = {
    MessageStatus.SENDING:          "⟳ Sending…",
    MessageStatus.ACCEPTED_BY_RADIO: "✓ Accepted by radio",
    MessageStatus.ACKNOWLEDGED:     "✓✓ Acknowledged",
    MessageStatus.FAILED:           "✕ Failed",
    MessageStatus.UNKNOWN_DELIVERY: "? Unknown delivery",
    MessageStatus.RECEIVED:         "",
}


class MessageBubble(QWidget):
    """One message bubble in the chat view."""

    def __init__(self, msg: ChatMessage, parent=None):
        super().__init__(parent)
        self._msg = msg
        self._status_lbl: QLabel | None = None
        self._build()

    def _build(self) -> None:
        is_out = self._msg.direction == MessageDirection.OUTBOUND
        is_sys = self._msg.direction == MessageDirection.SYSTEM

        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 2, 6, 2)
        outer.setSpacing(0)

        bubble = QWidget()
        bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        bubble.setMaximumWidth(600)

        if is_sys:
            bubble.setObjectName("bubbleSystem")
        elif is_out:
            bubble.setObjectName("bubbleOut")
        else:
            bubble.setObjectName("bubbleIn")

        inner = QVBoxLayout(bubble)
        inner.setContentsMargins(10, 6, 10, 6)
        inner.setSpacing(2)

        # Sender row
        if not is_sys:
            sender_row = QHBoxLayout()
            sender_row.setContentsMargins(0, 0, 0, 0)
            sender = QLabel(self._msg.sender_name)
            sender.setObjectName("senderLabel" if is_out else "senderLabelIn")
            # Same rationale as text_lbl below — mesh-controlled string.
            sender.setTextFormat(Qt.TextFormat.PlainText)
            sender_row.addWidget(sender)
            sender_row.addStretch()

            ts_local = self._msg.timestamp.astimezone(tz=None)
            ts_str = ts_local.strftime("%H:%M")
            ts_lbl = QLabel(ts_str)
            ts_lbl.setObjectName("msgMeta")
            sender_row.addWidget(ts_lbl)
            inner.addLayout(sender_row)

        # Message text (selectable). Explicit PlainText format — this is
        # untrusted content from the mesh network, and QLabel's default
        # AutoText heuristic would otherwise interpret HTML-tag-like
        # content in a received message as actual markup.
        text_lbl = QLabel(self._msg.text)
        text_lbl.setObjectName("msgText")
        text_lbl.setWordWrap(True)
        text_lbl.setTextFormat(Qt.TextFormat.PlainText)
        text_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        inner.addWidget(text_lbl)

        # Outbound status
        if is_out:
            self._status_lbl = QLabel(_STATUS_TEXT.get(self._msg.status, ""))
            self._status_lbl.setObjectName("statusLabel")
            self._status_lbl.setProperty("status", self._msg.status.value)
            self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            inner.addWidget(self._status_lbl)

        if is_out:
            outer.addStretch()
            outer.addWidget(bubble)
        else:
            outer.addWidget(bubble)
            outer.addStretch()

    def update_status(self, status: MessageStatus) -> None:
        if self._status_lbl is None:
            return
        self._status_lbl.setText(_STATUS_TEXT.get(status, ""))
        self._status_lbl.setProperty("status", status.value)
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)
