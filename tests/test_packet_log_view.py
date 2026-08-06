"""Tests for PacketLogModel.

Previously backed by a deque(maxlen=_MAX_ROWS): data() converted the whole
deque to a list on every single cell access (O(n) per cell, not just per
row), and append_packet() at capacity relied on the deque silently evicting
the oldest row while only ever sending Qt an insert notification — never a
matching removal — leaving the view thinking one more row existed than
rowCount() actually reported once the log filled up.
"""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt

import meshchat.ui.monitor.packet_log_view as packet_log_view
from meshchat.models.network_packet import NetworkPacket
from meshchat.ui.monitor.packet_log_view import PacketLogModel


def _pkt(sender_num: int) -> NetworkPacket:
    return NetworkPacket(
        session_id="s",
        observed_at=datetime.now(timezone.utc),
        rx_time=None,
        sender_num=sender_num,
        sender_id=None,
        destination_num=None,
        packet_id=None,
        channel_index=0,
        portnum=1,
        portnum_name="TEXT_MESSAGE_APP",
        text=None,
        payload_size=None,
        rx_snr=None,
        rx_rssi=None,
        hop_start=None,
        hop_limit=None,
        hops_used=None,
        via_mqtt=False,
        transport_mechanism=None,
        pki_encrypted=False,
        want_ack=None,
        priority=None,
        raw_metadata_json="{}",
    )


class TestPacketLogModel:
    def test_data_returns_the_right_row(self):
        model = PacketLogModel()
        for i in range(5):
            model.append_packet(_pkt(100 + i))
        idx = model.index(3, 2)  # column 2 = Sender
        assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "!00000067"  # 103 in hex

    def test_sender_num_zero_is_shown_not_treated_as_missing(self):
        # `if pkt.sender_num:` treated node 0 (a legitimate node number) the
        # same as no sender at all, showing "?" instead of "!00000000".
        model = PacketLogModel()
        model.append_packet(_pkt(0))
        idx = model.index(0, 2)  # column 2 = Sender
        assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "!00000000"

    def test_row_count_matches_appended_packets_below_cap(self):
        model = PacketLogModel()
        for i in range(50):
            model.append_packet(_pkt(i))
        assert model.rowCount() == 50

    def test_row_count_stays_at_cap_once_exceeded(self):
        model = PacketLogModel()
        cap = 5
        original_cap = packet_log_view._MAX_ROWS
        packet_log_view._MAX_ROWS = cap
        try:
            for i in range(cap + 10):
                model.append_packet(_pkt(i))
            # The exact bug this guards against: rowCount() must reflect
            # reality, not silently drift from what Qt was told via
            # begin/endInsertRows without a matching removal.
            assert model.rowCount() == cap
            assert len(model._rows) == cap
        finally:
            packet_log_view._MAX_ROWS = original_cap

    def test_oldest_row_evicted_first(self):
        original_cap = packet_log_view._MAX_ROWS
        packet_log_view._MAX_ROWS = 3
        try:
            model = PacketLogModel()
            for i in range(5):
                model.append_packet(_pkt(i))
            # Packets 0 and 1 should have been evicted; 2, 3, 4 remain, oldest first.
            remaining = [pkt.sender_num for pkt in model._rows]
            assert remaining == [2, 3, 4]
        finally:
            packet_log_view._MAX_ROWS = original_cap

    def test_data_out_of_range_returns_none_not_crash(self):
        model = PacketLogModel()
        model.append_packet(_pkt(1))
        idx = model.index(5, 0)
        assert model.data(idx, Qt.ItemDataRole.DisplayRole) is None

    def test_clear_resets_row_count(self):
        model = PacketLogModel()
        for i in range(10):
            model.append_packet(_pkt(i))
        model.clear()
        assert model.rowCount() == 0
