"""Asking a radio whom it is — the one place in this package that opens anything.

`registry.py` deliberately never opens a port, so a refresh is instant and safe. This is
the deliberate exception: identification means connecting, and connecting is expensive in
a way that has been measured rather than guessed.

* A serial connect costs **13.6 s** on this bench with another radio already up, so the
  default timeout is set above that rather than at some round number below it.
* A device that is not a radio **will not answer**, and the handshake simply expires. This
  machine has one attached, explicitly off-limits.

So identification is always an explicit act by a caller that has decided it wants to spend
that time, never something a list refresh triggers. When it fails, the failure is reported
as *unidentified* rather than as a broken radio: nothing here can tell "not a radio" apart
from "a radio that is unwell", and pretending otherwise would put a wrong diagnosis in
front of a user.

The caller is responsible for holding the lease first (`lease.acquire_radio`). That is not
enforced because the lease is per radio and a transport can be identified before its node
number is known, which is the point at which the two are joined — `lease.add_identity`.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .base import BLE, SERIAL, RadioTransport, candidate_key

log = logging.getLogger(__name__)

#: Above the measured 13.6 s serial connect, with room for a slower one. A timeout of ten
#: seconds would report healthy radios as unidentified, which is worse than waiting.
CONNECT_TIMEOUT_S = 45.0


class RadioUnidentified(RuntimeError):
    """Raised when a radio did not say who it is."""


@dataclass(frozen=True)
class RadioInfo:
    """A radio that has answered for itself."""

    node_num: int
    long_name: str
    short_name: str
    hardware: str
    firmware: str
    transport: RadioTransport

    @property
    def key(self) -> str:
        """The mesh's own name for this radio — the strongest identity there is."""
        return candidate_key(node_num=self.node_num)

    def describe(self) -> str:
        shown = self.long_name or self.short_name or str(self.node_num)
        return f"{shown} ({self.hardware}, fw {self.firmware or 'unknown'})"

    def as_transport_line(self) -> str:
        return f"{self.describe()} via {self.transport.describe()}"


def _imported_opener(transport: RadioTransport, timeout_s: float) -> Any:
    """Open the interface for a transport, importing the library only when needed.

    Imported inside the function so the rest of this package stays importable — and
    testable — without a Meshtastic stack present.
    """
    if transport.kind == SERIAL:
        from meshtastic.serial_interface import SerialInterface

        return SerialInterface(devPath=transport.address)
    if transport.kind == BLE:
        from meshtastic.ble_interface import BLEInterface

        return BLEInterface(address=transport.address)
    raise RadioUnidentified(
        f"{transport.kind} is not a transport this code knows how to identify a radio on",
    )


def identify(
    transport: RadioTransport,
    *,
    timeout_s: float = CONNECT_TIMEOUT_S,
    opener: Callable[[RadioTransport, float], Any] | None = None,
) -> RadioInfo:
    """Open `transport`, ask the radio who it is, and close it.

    Raises `RadioUnidentified` rather than returning None, because every caller has a
    different reason to want to know and none of them wants a silent nothing back.
    """
    open_it = opener or _imported_opener
    interface: Any = None
    try:
        interface = open_it(transport, timeout_s)
        info = interface.getMyNodeInfo() or {}
        user = info.get("user", {})
        metadata = interface.metadata
        node_num = int(interface.myInfo.my_node_num)
        return RadioInfo(
            node_num=node_num,
            long_name=str(user.get("longName") or ""),
            short_name=str(user.get("shortName") or ""),
            hardware=str(user.get("hwModel") or ""),
            # The protobuf, not a dict — reached by attribute. `metadata` is not a mapping
            # here, and calling .get() on it raises rather than answering.
            firmware=str(getattr(metadata, "firmware_version", "") or ""),
            transport=transport,
        )
    except RadioUnidentified:
        raise
    except Exception as exc:
        # Deliberately broad. A device that is not a radio fails in the library's own way —
        # a timeout waiting for a handshake that never comes — and there is no useful
        # distinction to draw between that and a radio that is unwell. Both are reported as
        # unidentified, with the underlying reason kept for the log.
        log.info("Could not identify %s: %s", transport.describe(), exc)
        raise RadioUnidentified(
            f"Nothing answered on {transport.address}. "
            "That can mean it is not a Meshtastic radio, that it is busy elsewhere, "
            "or that it is a radio not currently running Meshtastic.",
        ) from exc
    finally:
        if interface is not None:
            try:
                interface.close()
            except Exception:  # pragma: no cover - closing a dead link is not news
                log.debug("Error closing %s after identifying", transport.address, exc_info=True)
