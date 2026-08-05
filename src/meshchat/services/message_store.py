"""MeshChat – services: in-memory message store."""
from __future__ import annotations

from collections import defaultdict

from meshchat.models.message import ChatMessage


class MessageStore:
    """
    Stores chat messages per channel in memory.

    Keyed by (connection_identity, channel_index).
    Thread safety: all mutations happen on the Qt GUI thread via signals.
    """

    def __init__(self) -> None:
        self._messages: dict[tuple[str, int], list[ChatMessage]] = defaultdict(list)
        self._identity: str = ""

    def set_identity(self, identity: str) -> None:
        self._identity = identity

    def clear(self) -> None:
        self._messages.clear()

    def add(self, msg: ChatMessage) -> None:
        key = (self._identity, msg.channel_index)
        self._messages[key].append(msg)

    def update_status(self, local_id: str, new_msg: ChatMessage) -> None:
        key = (self._identity, new_msg.channel_index)
        lst = self._messages[key]
        for i, m in enumerate(lst):
            if m.local_id == local_id:
                lst[i] = new_msg
                return

    def get_channel(self, channel_index: int) -> list[ChatMessage]:
        return list(self._messages.get((self._identity, channel_index), []))

    def all_channels(self) -> list[int]:
        return [ch for (_, ch) in self._messages.keys() if _ == self._identity]
