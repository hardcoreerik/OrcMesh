"""Tests for ExportService.export_packets_csv().

Several numeric fields used `pkt.field or ""` instead of the `is not
None` pattern the neighboring fields in the same row already used — a
legitimate 0 (a node number, packet id, portnum, or payload size can all
genuinely be 0) was silently blanked out to an empty CSV cell instead of
showing the real value.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone

from meshchat.models.network_packet import NetworkPacket
from meshchat.services.export_service import ExportService


def _pkt(**overrides) -> NetworkPacket:
    defaults = dict(
        session_id="s",
        observed_at=datetime.now(timezone.utc),
        rx_time=None,
        sender_num=1,
        sender_id=None,
        destination_num=None,
        packet_id=1,
        channel_index=0,
        portnum=1,
        portnum_name="TEXT_MESSAGE_APP",
        text=None,
        payload_size=1,
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
    defaults.update(overrides)
    return NetworkPacket(**defaults)


def _export_and_read_row(tmp_path, pkt) -> dict:
    path = tmp_path / "out.csv"
    ExportService.export_packets_csv([pkt], path)
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    return rows[0]


class TestZeroValuesSurviveExport:
    def test_sender_num_zero_is_not_blanked(self, tmp_path):
        row = _export_and_read_row(tmp_path, _pkt(sender_num=0))
        assert row["sender_num"] == "0"

    def test_destination_num_zero_is_not_blanked(self, tmp_path):
        row = _export_and_read_row(tmp_path, _pkt(destination_num=0))
        assert row["destination_num"] == "0"

    def test_packet_id_zero_is_not_blanked(self, tmp_path):
        row = _export_and_read_row(tmp_path, _pkt(packet_id=0))
        assert row["packet_id"] == "0"

    def test_portnum_zero_is_not_blanked(self, tmp_path):
        row = _export_and_read_row(tmp_path, _pkt(portnum=0))
        assert row["portnum"] == "0"

    def test_payload_size_zero_is_not_blanked(self, tmp_path):
        row = _export_and_read_row(tmp_path, _pkt(payload_size=0))
        assert row["payload_size"] == "0"

    def test_none_values_still_render_as_empty(self, tmp_path):
        row = _export_and_read_row(
            tmp_path,
            _pkt(sender_num=None, destination_num=None, packet_id=None, portnum=None, payload_size=None),
        )
        assert row["sender_num"] == ""
        assert row["destination_num"] == ""
        assert row["packet_id"] == ""
        assert row["portnum"] == ""
        assert row["payload_size"] == ""


class TestViaMqttTriState:
    def test_via_mqtt_false_exports_as_zero(self, tmp_path):
        row = _export_and_read_row(tmp_path, _pkt(via_mqtt=False))
        assert row["via_mqtt"] == "0"

    def test_via_mqtt_true_exports_as_one(self, tmp_path):
        row = _export_and_read_row(tmp_path, _pkt(via_mqtt=True))
        assert row["via_mqtt"] == "1"

    def test_via_mqtt_unknown_exports_as_empty_not_zero(self, tmp_path):
        # via_mqtt is bool | None — older firmware never reports it at all.
        # int(bool(None)) == 0 used to collapse that into "confirmed not
        # MQTT", indistinguishable from a real False.
        row = _export_and_read_row(tmp_path, _pkt(via_mqtt=None))
        assert row["via_mqtt"] == ""
