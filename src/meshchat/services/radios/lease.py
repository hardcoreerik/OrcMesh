"""Who is holding which radio.

The SDR side needs this because a dongle can only be open in one process. The radio side
needs it for a subtler reason, and it is the one measured on this bench: **one radio
usually has more than one door.** The Heltec answers on ``COM24`` and on BLE at the same
time, and the operating system only refuses the second open of the same *port*, so a
lease keyed on the transport would happily let a serial session and a BLE session both
drive one radio. That is not two radios; it is one radio being written to from two
directions.

The lease is therefore keyed on the radio (`base.candidate_key`), never on the port, and
an owner may hold several radios at once — which is the entire point of supporting more
than one.

**Take the lease before connecting, and give it back on every path.** A lease that
outlives a failed connect makes a radio read as busy for the rest of the session with
nothing running and nothing for the user to stop. That bug existed on the SDR side and
was fixed by hand at each call site; `hold()` is here so it cannot be forgotten this
time.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)


class RadioBusy(RuntimeError):
    """Raised when a radio is already held by someone else."""


#: radio key -> (owning token, human label)
_radio_owners: dict[str, tuple[str, str]] = {}
_radio_lock = threading.Lock()


def acquire_radio(owner: str, label: str, key: str) -> tuple[bool, str]:
    """Claim one radio for `owner`, naming it `label` in any complaint.

    Re-acquiring as the same owner succeeds, so a reconnect inside one feature does not
    have to release first. A different owner is refused whatever transport it intends to
    use, which is the whole reason the key is the radio and not the port.
    """
    with _radio_lock:
        held = _radio_owners.get(key)
        if held is not None and held[0] != owner:
            return False, (
                f"{key.removeprefix('radio:')} is already in use by {held[1]}.\n\n"
                "A radio can be driven from one place at a time, and it answers on more "
                "than one (serial and Bluetooth reach the same board, not two radios). "
                "Disconnect it there first, or use the other radio."
            )
        _radio_owners[key] = (owner, label)
        return True, ""


def release_radio(owner: str, key: str) -> None:
    """Give the radio back. Releasing someone else's lease does nothing."""
    with _radio_lock:
        held = _radio_owners.get(key)
        if held is not None and held[0] == owner:
            del _radio_owners[key]


def radio_owner(key: str) -> str:
    """Label of whatever is holding that radio, or an empty string."""
    with _radio_lock:
        held = _radio_owners.get(key)
        return held[1] if held is not None else ""


def held_radios() -> dict[str, str]:
    """Every held radio, key to label, for a diagnostics view."""
    with _radio_lock:
        return {key: label for key, (_, label) in _radio_owners.items()}


def owner_radios(owner: str) -> list[str]:
    """The radios one owner holds — which for a multi-radio session is more than one."""
    with _radio_lock:
        return [key for key, (token, _) in _radio_owners.items() if token == owner]


@contextmanager
def hold(owner: str, label: str, key: str) -> Iterator[None]:
    """Hold a radio for the duration of a block, and give it back however the block ends.

    Raises if the radio is held elsewhere, so the caller does not have to check and then
    remember to release on the failure paths — which is the mistake this exists to
    prevent. Connecting is the thing most likely to raise, and it happens inside the
    block.
    """
    acquired, complaint = acquire_radio(owner, label, key)
    if not acquired:
        raise RadioBusy(complaint)
    try:
        yield
    finally:
        release_radio(owner, key)
