"""Build and apply secret-safe Meshtastic configuration snapshots."""
from __future__ import annotations

import re
from typing import Any

from google.protobuf.descriptor import FieldDescriptor

from meshchat.models.device_control import (
    ChannelControl,
    ConfigChoice,
    ConfigField,
    ConfigSection,
    DeviceControlSnapshot,
)


_WRITE_ONLY = {
    ("network", "wifi_psk"),
    ("mqtt", "password"),
    ("bluetooth", "fixed_pin"),
}
_READ_ONLY = {
    ("security", "public_key"),
    ("security", "private_key"),
    ("security", "admin_key"),
}
_SKIP_SECTIONS = {"version"}
_SKIP_FIELDS = {"version"}
_WRITABLE_SECTIONS = {
    "device", "position", "power", "network", "display", "lora", "bluetooth", "security",
    "mqtt", "serial", "external_notification", "store_forward", "range_test", "telemetry",
    "canned_message", "audio", "remote_hardware", "neighbor_info", "ambient_lighting",
    "detection_sensor", "paxcounter", "traffic_management",
}

_INT_TYPES = {
    FieldDescriptor.TYPE_INT32,
    FieldDescriptor.TYPE_INT64,
    FieldDescriptor.TYPE_UINT32,
    FieldDescriptor.TYPE_UINT64,
    FieldDescriptor.TYPE_SINT32,
    FieldDescriptor.TYPE_SINT64,
    FieldDescriptor.TYPE_FIXED32,
    FieldDescriptor.TYPE_FIXED64,
    FieldDescriptor.TYPE_SFIXED32,
    FieldDescriptor.TYPE_SFIXED64,
}
_FLOAT_TYPES = {FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE}


def _label(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip().title()


def _field_kind(field: FieldDescriptor) -> str | None:
    if field.type == FieldDescriptor.TYPE_BOOL:
        return "bool"
    if field.type == FieldDescriptor.TYPE_ENUM:
        return "enum"
    if field.type in _INT_TYPES:
        return "int"
    if field.type in _FLOAT_TYPES:
        return "float"
    if field.type == FieldDescriptor.TYPE_STRING:
        return "string"
    if field.type == FieldDescriptor.TYPE_BYTES:
        return "bytes"
    return None


def _section(name: str, message) -> ConfigSection:
    fields: list[ConfigField] = []
    for descriptor in message.DESCRIPTOR.fields:
        if descriptor.name in _SKIP_FIELDS:
            continue
        kind = _field_kind(descriptor)
        if kind is None:
            continue
        write_only = (name, descriptor.name) in _WRITE_ONLY
        read_only = (name, descriptor.name) in _READ_ONLY or kind == "bytes"
        raw = getattr(message, descriptor.name)
        if write_only or read_only:
            value: Any = ""
        elif descriptor.is_repeated:
            value = list(raw)
        else:
            value = raw
        choices: tuple[ConfigChoice, ...] = ()
        if descriptor.enum_type is not None:
            choices = tuple(ConfigChoice(v.name, v.number) for v in descriptor.enum_type.values)
        fields.append(ConfigField(
            name=descriptor.name,
            label=_label(descriptor.name),
            kind=kind,
            value=value,
            choices=choices,
            repeated=descriptor.is_repeated,
            write_only=write_only,
            read_only=read_only,
        ))
    return ConfigSection(name=name, label=_label(name), fields=tuple(fields))


def build_snapshot(interface, serial_port: str | None) -> DeviceControlSnapshot:
    node = interface.localNode
    my_info = getattr(interface, "myInfo", None)
    metadata = getattr(interface, "metadata", None)
    local_num = getattr(my_info, "my_node_num", None)
    local = (getattr(interface, "nodesByNum", {}) or {}).get(local_num, {})
    user = local.get("user", {}) if isinstance(local, dict) else {}

    sections: list[ConfigSection] = []
    for parent in (getattr(node, "localConfig", None), getattr(node, "moduleConfig", None)):
        if parent is None:
            continue
        for descriptor in parent.DESCRIPTOR.fields:
            if (
                descriptor.name in _SKIP_SECTIONS
                or descriptor.name not in _WRITABLE_SECTIONS
                or descriptor.type != FieldDescriptor.TYPE_MESSAGE
            ):
                continue
            sections.append(_section(descriptor.name, getattr(parent, descriptor.name)))

    channels: list[ChannelControl] = []
    from meshtastic.protobuf import channel_pb2
    for channel in getattr(node, "channels", None) or []:
        try:
            role_name = channel_pb2.Channel.Role.Name(channel.role)
        except ValueError:
            role_name = "UNKNOWN"
        settings = channel.settings
        channels.append(ChannelControl(
            index=int(channel.index),
            role=int(channel.role),
            role_name=role_name,
            name=settings.name,
            uplink_enabled=bool(settings.uplink_enabled),
            downlink_enabled=bool(settings.downlink_enabled),
            position_precision=int(settings.module_settings.position_precision),
        ))

    usb_port = None
    if serial_port:
        from serial.tools import list_ports
        usb_port = next((port for port in list_ports.comports() if port.device == serial_port), None)
    return DeviceControlSnapshot(
        node_id=user.get("id"),
        long_name=user.get("longName") or "",
        short_name=user.get("shortName") or "",
        hw_model=user.get("hwModel") or getattr(metadata, "hw_model", "") or "",
        firmware_version=getattr(metadata, "firmware_version", "") or "",
        pio_env=getattr(my_info, "pio_env", "") or "",
        serial_port=serial_port,
        usb_vid=getattr(usb_port, "vid", None),
        usb_pid=getattr(usb_port, "pid", None),
        usb_serial=getattr(usb_port, "serial_number", None),
        can_shutdown=bool(getattr(metadata, "can_shutdown", False)),
        has_wifi=bool(getattr(metadata, "has_wifi", False)),
        has_bluetooth=bool(getattr(metadata, "has_bluetooth", False)),
        sections=tuple(sections),
        channels=tuple(channels),
    )


def _coerce(field: FieldDescriptor, value: Any) -> Any:
    if field.is_repeated:
        if not isinstance(value, list):
            raise ValueError(f"{field.name} must be a list")
        return [_coerce_scalar(field, item) for item in value]
    return _coerce_scalar(field, value)


def _coerce_scalar(field: FieldDescriptor, value: Any) -> Any:
    if field.type == FieldDescriptor.TYPE_BOOL:
        return bool(value)
    if field.type == FieldDescriptor.TYPE_ENUM:
        number = int(value)
        if field.enum_type.values_by_number.get(number) is None:
            raise ValueError(f"invalid value for {field.name}")
        return number
    if field.type in _INT_TYPES:
        return int(value)
    if field.type in _FLOAT_TYPES:
        return float(value)
    if field.type == FieldDescriptor.TYPE_STRING:
        return str(value)
    raise ValueError(f"{field.name} is not editable")


def apply_section(node, section_name: str, changes: dict[str, Any]) -> None:
    parent = node.localConfig if hasattr(node.localConfig, section_name) else node.moduleConfig
    if not hasattr(parent, section_name):
        raise ValueError(f"Unknown configuration section: {section_name}")
    section = getattr(parent, section_name)
    fields = section.DESCRIPTOR.fields_by_name
    for name, value in changes.items():
        field = fields.get(name)
        if field is None or (section_name, name) in _READ_ONLY or field.type == FieldDescriptor.TYPE_BYTES:
            raise ValueError(f"{name} is not editable")
        if (section_name, name) in _WRITE_ONLY and value in (None, ""):
            continue
        coerced = _coerce(field, value)
        if field.is_repeated:
            target = getattr(section, name)
            del target[:]
            target.extend(coerced)
        else:
            setattr(section, name, coerced)
    node.writeConfig(section_name)
