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
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class RadioBusy(RuntimeError):
    """Raised when a radio is already held by someone else."""


@dataclass
class _Lease:
    """One radio, held by one owner, under every name it is known by.

    A set of keys rather than a single key, because a radio's identity is *learned in
    stages*. It is leased under its hardware address before anything is connected, and only
    after connecting does it report the node number the mesh knows it by. If those were two
    separate leases, they would be two things to hold and two things to forget.
    """

    owner: str
    label: str
    keys: set[str] = field(default_factory=set)


#: radio key -> the lease covering it. Every key of a lease points at the same object, so
#: releasing by any of them releases the radio.
_leases: dict[str, _Lease] = {}
_radio_lock = threading.Lock()


def _lease_for(key: str) -> _Lease | None:
    return _leases.get(key)


def acquire_radio(owner: str, label: str, key: str) -> tuple[bool, str]:
    """Claim one radio for `owner`, naming it `label` in any complaint.

    Re-acquiring as the same owner succeeds, so a reconnect inside one feature does not
    have to release first. A different owner is refused whatever transport it intends to
    use, which is the whole reason the key is the radio and not the port.
    """
    with _radio_lock:
        held = _lease_for(key)
        if held is not None and held.owner != owner:
            return False, (
                f"{key.removeprefix('radio:')} is already in use by {held.label}.\n\n"
                "A radio can be driven from one place at a time, and it answers on more "
                "than one (serial and Bluetooth reach the same board, not two radios). "
                "Disconnect it there first, or use the other radio."
            )
        if held is not None:
            held.label = label
            return True, ""
        _leases[key] = _Lease(owner=owner, label=label, keys={key})
        return True, ""


def add_identity(owner: str, key: str, identity: str) -> tuple[bool, str]:
    """Extend the lease on `key` to also cover `identity`, once the radio reveals it.

    Called after a connect, when the radio reports the node number the mesh knows it by.
    Holding it matters for the transports that can only say a node number — TCP reaches a
    radio at an address that has nothing to do with its hardware — so without this a second
    session could take the radio through a door the first session's key does not cover.

    **The window before this runs is real and is documented rather than hidden.** Between
    acquiring and identifying, only the hardware address is held. That is sufficient for
    every transport on this bench, because the registry keys Bluetooth by its MAC family
    too, so both doors collide on the address alone. It is not sufficient for a transport
    that can only report a node number, which is precisely what this call is for.
    """
    with _radio_lock:
        held = _lease_for(key)
        if held is None or held.owner != owner:
            return False, "that radio is not held by this session"
        other = _lease_for(identity)
        if other is not None and other is not held:
            return False, (
                f"{identity.removeprefix('radio:')} is already in use by {other.label}.\n\n"
                "This is the same radio under the name the mesh knows it by."
            )
        held.keys.add(identity)
        _leases[identity] = held
        return True, ""


def release_radio(owner: str, key: str) -> None:
    """Give the radio back, under every name it is known by.

    Released by any of its keys and released entirely, because they are one radio: leaving
    the node-number key held after the address key was freed would keep it reading as busy
    under a name the user never saw.
    """
    with _radio_lock:
        held = _lease_for(key)
        if held is None or held.owner != owner:
            return
        for held_key in held.keys:
            _leases.pop(held_key, None)


def radio_owner(key: str) -> str:
    """Label of whatever is holding that radio, or an empty string."""
    with _radio_lock:
        held = _lease_for(key)
        return held.label if held is not None else ""


def held_radios() -> dict[str, str]:
    """Every held radio, key to label, for a diagnostics view."""
    with _radio_lock:
        return {key: lease.label for key, lease in _leases.items()}


def owner_radios(owner: str) -> list[str]:
    """The radios one owner holds, one entry per radio rather than per name.

    A radio known by two names is still one radio, so this de-duplicates by lease identity.
    Within a lease it reports the alphabetically first key, which makes the answer stable
    for a caller comparing two lists rather than depending on insertion order.
    """
    with _radio_lock:
        counted: list[_Lease] = []
        keys: list[str] = []
        for key, held in _leases.items():
            if held.owner != owner or any(held is seen for seen in counted):
                continue
            counted.append(held)
            keys.append(sorted(held.keys)[0])
        return sorted(keys)


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
