"""MeshChat – ExportService: CSV / JSON export helpers."""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Iterable

from meshchat.models.network_packet import NetworkPacket
from meshchat.models.node_snapshot import NodeSnapshot

log = logging.getLogger(__name__)

_PACKET_FIELDS = [
    "observed_at", "rx_time", "sender_num", "sender_id",
    "destination_num", "packet_id", "channel_index",
    "portnum", "portnum_name", "payload_size",
    "rx_snr", "rx_rssi", "hop_start", "hop_limit", "hops_used",
    "via_mqtt", "transport_mechanism",
]


class ExportService:
    """Write filtered data to CSV or JSON files."""

    @staticmethod
    def export_packets_csv(
        packets: Iterable[NetworkPacket],
        path: Path,
        include_text: bool = False,
    ) -> int:
        """
        Export packets to CSV. Message text is excluded by default.
        Returns number of rows written.
        """
        rows = 0
        with path.open("w", newline="", encoding="utf-8") as f:
            fields = list(_PACKET_FIELDS)
            if include_text:
                fields.append("text")
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for pkt in packets:
                row = {
                    "observed_at":        pkt.observed_at.isoformat(),
                    "rx_time":            pkt.rx_time.isoformat() if pkt.rx_time else "",
                    # is-not-None, not `or ""`, for every numeric field
                    # below: 0 is a legitimate value for a node number,
                    # packet id, portnum (UNKNOWN_APP), or payload size —
                    # `or ""` was silently blanking those rows out to an
                    # empty CSV cell instead of showing the real 0.
                    "sender_num":         pkt.sender_num if pkt.sender_num is not None else "",
                    "sender_id":          pkt.sender_id or "",
                    "destination_num":    pkt.destination_num if pkt.destination_num is not None else "",
                    "packet_id":          pkt.packet_id if pkt.packet_id is not None else "",
                    "channel_index":      pkt.channel_index if pkt.channel_index is not None else "",
                    "portnum":            pkt.portnum if pkt.portnum is not None else "",
                    "portnum_name":       pkt.portnum_name,
                    "payload_size":       pkt.payload_size if pkt.payload_size is not None else "",
                    "rx_snr":             pkt.rx_snr if pkt.rx_snr is not None else "",
                    "rx_rssi":            pkt.rx_rssi if pkt.rx_rssi is not None else "",
                    "hop_start":          pkt.hop_start if pkt.hop_start is not None else "",
                    "hop_limit":          pkt.hop_limit if pkt.hop_limit is not None else "",
                    "hops_used":          pkt.hops_used if pkt.hops_used is not None else "",
                    "via_mqtt":           int(bool(pkt.via_mqtt)),
                    "transport_mechanism": pkt.transport_mechanism or "",
                }
                if include_text:
                    row["text"] = pkt.text or ""
                writer.writerow(row)
                rows += 1
        log.info("Exported %d packet rows to %s", rows, path)
        return rows

    @staticmethod
    def export_nodes_csv(
        nodes: Iterable[NodeSnapshot],
        path: Path,
    ) -> int:
        rows = 0
        fields = [
            "node_num", "node_id", "long_name", "short_name",
            "role", "hw_model", "first_seen", "last_heard",
            "packet_count", "text_count", "rf_count", "via_mqtt_count",
            "last_snr", "last_rssi", "last_hops_used",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for n in nodes:
                writer.writerow({
                    "node_num":      n.node_num,
                    "node_id":       n.node_id or "",
                    "long_name":     n.long_name or "",
                    "short_name":    n.short_name or "",
                    "role":          n.role or "",
                    "hw_model":      n.hw_model or "",
                    "first_seen":    n.first_seen.isoformat() if n.first_seen else "",
                    "last_heard":    n.last_heard.isoformat() if n.last_heard else "",
                    "packet_count":  n.packet_count,
                    "text_count":    n.text_count,
                    "rf_count":      n.rf_count,
                    "via_mqtt_count": n.via_mqtt_count,
                    "last_snr":      n.last_snr if n.last_snr is not None else "",
                    "last_rssi":     n.last_rssi if n.last_rssi is not None else "",
                    "last_hops_used": n.last_hops_used if n.last_hops_used is not None else "",
                })
                rows += 1
        log.info("Exported %d node rows to %s", rows, path)
        return rows

    @staticmethod
    def export_session_json(
        session,
        path: Path,
        stats: dict | None = None,
    ) -> None:
        from meshchat.version import VERSION
        import meshtastic
        data = {
            "app_version":      VERSION,
            "meshtastic_version": getattr(meshtastic, "__version__", "?"),
            "session_id":       session.id,
            "transport":        session.transport,
            "connection_target": session.connection_target,
            "started_at":       session.started_at.isoformat(),
            "ended_at":         session.ended_at.isoformat() if session.ended_at else None,
            "local_node_id":    session.local_node_id,
            "packet_count":     session.packet_count,
            "stats":            stats or {},
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.info("Session JSON exported to %s", path)
