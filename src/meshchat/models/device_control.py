"""Typed, secret-safe models for connected-radio controls."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConfigChoice:
    label: str
    value: int


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    kind: str
    value: Any
    choices: tuple[ConfigChoice, ...] = ()
    repeated: bool = False
    write_only: bool = False
    read_only: bool = False


@dataclass(frozen=True)
class ConfigSection:
    name: str
    label: str
    fields: tuple[ConfigField, ...]


@dataclass(frozen=True)
class ChannelControl:
    index: int
    role: int
    role_name: str
    name: str
    uplink_enabled: bool
    downlink_enabled: bool
    position_precision: int


@dataclass(frozen=True)
class DeviceControlSnapshot:
    node_id: str | None
    long_name: str
    short_name: str
    hw_model: str
    firmware_version: str
    pio_env: str
    serial_port: str | None
    usb_vid: int | None
    usb_pid: int | None
    usb_serial: str | None
    can_shutdown: bool
    has_wifi: bool
    has_bluetooth: bool
    sections: tuple[ConfigSection, ...] = field(default_factory=tuple)
    channels: tuple[ChannelControl, ...] = field(default_factory=tuple)
