"""MeshChat for Windows – connection and device models."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    SYNCING = "syncing"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


class ErrorCode(Enum):
    BLE_NOT_AVAILABLE = "ble_not_available"
    BLE_DEVICE_NOT_FOUND = "ble_device_not_found"
    BLE_PAIRING_REQUIRED = "ble_pairing_required"
    BLE_CONNECTION_FAILED = "ble_connection_failed"
    TCP_HOST_NOT_FOUND = "tcp_host_not_found"
    TCP_REFUSED = "tcp_refused"
    TCP_TIMEOUT = "tcp_timeout"
    CONFIG_SYNC_TIMEOUT = "config_sync_timeout"
    CONNECTION_LOST = "connection_lost"
    SEND_FAILED = "send_failed"
    INVALID_MESSAGE = "invalid_message"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class UserFacingError:
    code: ErrorCode
    title: str
    message: str
    technical_detail: str | None
    recoverable: bool


@dataclass(frozen=True)
class BleDeviceSummary:
    name: str
    address: str


@dataclass(frozen=True)
class DeviceSummary:
    node_id: str
    node_num: int
    long_name: str
    short_name: str
    hw_model: str
    firmware_version: str
    transport: str


@dataclass(frozen=True)
class ChannelSummary:
    index: int
    name: str
    role: str


@dataclass(frozen=True)
class NodeSummary:
    node_num: int
    node_id: str
    long_name: str
    short_name: str
    role: str
    hw_model: str
