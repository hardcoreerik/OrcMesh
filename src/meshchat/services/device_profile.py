"""Meshtastic-compatible local configuration backup and restore."""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path


_MAX_PROFILE_BYTES = 2 * 1024 * 1024


def save_profile(interface, destination: Path) -> str:
    from meshtastic.__main__ import export_profile

    destination = destination.resolve()
    if destination.suffix.lower() != ".cfg" or not destination.parent.is_dir():
        raise ValueError("Choose a .cfg backup file in an existing folder")
    raw = export_profile(interface)
    if not raw or len(raw) > _MAX_PROFILE_BYTES:
        raise ValueError("The device returned an invalid configuration profile")
    partial = destination.with_suffix(".cfg.partial")
    try:
        partial.write_bytes(raw)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def load_profile(source: Path):
    from meshtastic.protobuf.clientonly_pb2 import DeviceProfile

    raw = source.read_bytes()
    if not raw or len(raw) > _MAX_PROFILE_BYTES:
        raise ValueError("The selected configuration profile is empty or too large")
    profile = DeviceProfile()
    profile.ParseFromString(raw)
    if not profile.ListFields():
        raise ValueError("The selected file is not a populated Meshtastic profile")
    return profile


def restore_profile(interface, source: Path) -> Path:
    source = source.resolve()
    if source.suffix.lower() != ".cfg" or not source.is_file():
        raise ValueError("Choose an existing Meshtastic .cfg profile")
    profile = load_profile(source)
    safety = source.with_name(
        f"{source.stem}.pre-restore-{datetime.now():%Y%m%d-%H%M%S}.cfg"
    )
    save_profile(interface, safety)

    node = interface.localNode
    node.beginSettingsTransaction()
    if profile.long_name or profile.short_name:
        node.setOwner(
            long_name=str(profile.long_name).strip() or None,
            short_name=str(profile.short_name).strip() or None,
        )
        time.sleep(0.5)
    if profile.channel_url:
        node.setURL(profile.channel_url)
        time.sleep(0.5)
    if profile.canned_messages:
        node.set_canned_message(profile.canned_messages)
        time.sleep(0.5)
    if profile.ringtone:
        node.set_ringtone(profile.ringtone)
        time.sleep(0.5)
    if (
        profile.HasField("fixed_position")
        and profile.HasField("config")
        and profile.config.HasField("position")
        and profile.config.position.fixed_position
    ):
        position = profile.fixed_position
        node.setFixedPosition(
            float(position.latitude_i * Decimal("1e-7")),
            float(position.longitude_i * Decimal("1e-7")),
            int(position.altitude),
        )
        time.sleep(0.5)
    if profile.HasField("config"):
        for field in profile.config.DESCRIPTOR.fields:
            if field.message_type is not None and profile.config.HasField(field.name):
                getattr(node.localConfig, field.name).CopyFrom(getattr(profile.config, field.name))
                node.writeConfig(field.name)
                time.sleep(0.5)
    if profile.HasField("module_config"):
        for field in profile.module_config.DESCRIPTOR.fields:
            if field.message_type is not None and profile.module_config.HasField(field.name):
                getattr(node.moduleConfig, field.name).CopyFrom(
                    getattr(profile.module_config, field.name)
                )
                node.writeConfig(field.name)
                time.sleep(0.5)
    node.commitSettingsTransaction()
    return safety
