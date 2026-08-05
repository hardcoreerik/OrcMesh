"""Fake Meshtastic interface for unit testing — no hardware required."""
from __future__ import annotations

import time
from typing import Callable

import threading
from pubsub import pub as _pub


class FakeMeshtasticInterface:
    """
    A minimal in-process fake for meshtastic.BLEInterface / TCPInterface.

    Supports:
    - Simulated connect (optional exception on construction)
    - Simulated disconnect
    - sendText that records calls (optional raise)
    - Triggering inbound packets via pump_text() / pump_node_info()
    - NodeDB seeding via nodes dict
    - Simulated ACK callback via ack_last()
    """

    def __init__(
        self,
        *,
        connect_raises: Exception | None = None,
        send_raises: Exception | None = None,
    ) -> None:
        self.connect_raises = connect_raises
        self.send_raises = send_raises

        # PubSub-style subscribers registered by the controller
        self._subscribers: dict[str, list[Callable]] = {}

        # Captured outbound sends
        self.sent_texts: list[dict] = []

        # Simulated device info
        self.myInfo = _FakeMyInfo(node_num=0x0001_B207)
        self.nodesByNum: dict[int, dict] = {
            0x0001_B207: {
                "num": 0x0001_B207,
                "user": {
                    "id": "!0001b207",
                    "longName": "Local Node",
                    "shortName": "LOC",
                },
            }
        }
        self.metadata = _FakeDeviceMetadata()
        self._closed = False

        if connect_raises is not None:
            raise connect_raises

        # Fire the connection event after a small delay so the caller's
        # `self._interface = BLEInterface(...)` assignment completes first.
        # The real Meshtastic library does this from a background thread.
        threading.Timer(0.05, self._fire_connected).start()

    def _fire_connected(self) -> None:
        if not self._closed:
            _pub.sendMessage("meshtastic.connection.established", interface=self)

    # ------------------------------------------------------------------
    # PubSub shim
    # ------------------------------------------------------------------

    def subscribe(self, topic: str, callback: Callable) -> None:
        self._subscribers.setdefault(topic, []).append(callback)

    def publish(self, topic: str, **kwargs) -> None:
        for cb in self._subscribers.get(topic, []):
            cb(**kwargs)

    # ------------------------------------------------------------------
    # Interface API
    # ------------------------------------------------------------------

    def sendText(
        self,
        text: str,
        destinationId: str = "^all",
        channelIndex: int = 0,
        wantAck: bool = True,
    ) -> None:
        if self.send_raises:
            raise self.send_raises
        packet_id = len(self.sent_texts) + 10_000
        self.sent_texts.append({
            "text": text,
            "destinationId": destinationId,
            "channelIndex": channelIndex,
            "wantAck": wantAck,
            "id": packet_id,
        })

    def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def pump_text(
        self,
        text: str,
        sender_num: int = 0x0001_0002,
        sender_id: str = "!00010002",
        channel: int = 0,
        snr: float = 8.5,
        rssi: int = -80,
        hop_start: int = 3,
        hop_limit: int = 3,
        via_mqtt: bool = False,
        packet_id: int | None = None,
    ) -> dict:
        """Simulate an inbound text packet via PyPubSub. Returns the raw packet dict."""
        pid = packet_id if packet_id is not None else int(time.time() * 1000) & 0xFFFF_FFFF
        raw = {
            "from": sender_num,
            "fromId": sender_id,
            "to": 0xFFFF_FFFF,
            "id": pid,
            "channel": channel,
            "rxSnr": snr,
            "rxRssi": rssi,
            "hopStart": hop_start,
            "hopLimit": hop_limit,
            "viaMqtt": via_mqtt,
            "decoded": {
                "portnum": 1,
                "text": text,
            },
        }
        # Use the real PyPubSub so the Meshtastic controller receives the event
        _pub.sendMessage("meshtastic.receive.text", packet=raw, interface=self)
        _pub.sendMessage("meshtastic.receive", packet=raw, interface=self)
        return raw

    def pump_node_info(
        self,
        sender_num: int = 0x0001_0003,
        long_name: str = "Remote Node",
        short_name: str = "REM",
        role: str = "CLIENT",
    ) -> dict:
        """Simulate a NodeInfo packet via PyPubSub."""
        raw = {
            "from": sender_num,
            "fromId": f"!{sender_num:08x}",
            "to": 0xFFFF_FFFF,
            "id": int(time.time() * 1000) & 0xFFFF_FFFF,
            "channel": 0,
            "rxSnr": 5.0,
            "rxRssi": -90,
            "hopStart": 3,
            "hopLimit": 2,
            "decoded": {
                "portnum": 4,
                "user": {
                    "id": f"!{sender_num:08x}",
                    "longName": long_name,
                    "shortName": short_name,
                    "role": role,
                },
            },
        }
        _pub.sendMessage("meshtastic.receive", packet=raw, interface=self)
        return raw

    def ack_last(self) -> None:
        """Simulate an ACK for the most-recently-sent packet via PyPubSub."""
        if not self.sent_texts:
            return
        last = self.sent_texts[-1]
        ack_pkt = {
            "from": 0xFFFF_FFFF,
            "id": last["id"] + 1,
            "decoded": {
                "portnum": 5,
                "routing": {"errorReason": "NONE"},
                "requestId": last["id"],
            },
        }
        _pub.sendMessage("meshtastic.receive", packet=ack_pkt, interface=self)

    def add_node(self, node_num: int, long_name: str, short_name: str) -> None:
        """Seed the fake NodeDB."""
        self.nodesByNum[node_num] = {
            "num": node_num,
            "user": {
                "id": f"!{node_num:08x}",
                "longName": long_name,
                "shortName": short_name,
            },
        }


class _FakeMyInfo:
    def __init__(self, node_num: int):
        self.myNodeNum = node_num        # camelCase (Meshtastic Python client)
        self.my_node_num = node_num      # snake_case (some versions / protobuf)


class _FakeDeviceMetadata:
    def __init__(self):
        self.firmwareVersion = "2.5.0.0"
        self.deviceStateVersion = 24
