"""MeshChat – services: persistent settings via QSettings."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSettings

APP_NAME = "MeshChat"
ORG_NAME = "MeshChat"

_DEFAULTS: dict[str, Any] = {
    "transport": "TCP",
    "tcp_host": "",
    "tcp_port": 4403,
    "ble_address": "",
    "selected_channel": 0,
    "theme": "dark",
    "message_limit_bytes": 200,
    "use_metric": True,
    "active_window_minutes": 60,
    "position_age_limit_minutes": 360,
}


def _qs() -> QSettings:
    return QSettings(ORG_NAME, APP_NAME)


def get(key: str, default: Any = None) -> Any:
    val = _qs().value(key, _DEFAULTS.get(key, default))
    # QSettings returns strings; coerce known types
    expected = _DEFAULTS.get(key)
    if isinstance(expected, bool):
        if isinstance(val, str):
            return val.lower() not in ("false", "0", "no", "")
        return bool(val)
    if isinstance(expected, int):
        try:
            return int(val)
        except (TypeError, ValueError):
            return expected
    return val


def set(key: str, value: Any) -> None:  # noqa: A001
    _qs().setValue(key, value)


def get_window_geometry() -> bytes | None:
    raw = _qs().value("window_geometry")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    return None


def save_window_geometry(geometry: bytes) -> None:
    _qs().setValue("window_geometry", geometry)


def get_splitter_state(name: str) -> bytes | None:
    raw = _qs().value(f"splitter_{name}")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    return None


def save_splitter_state(name: str, state: bytes) -> None:
    _qs().setValue(f"splitter_{name}", state)
