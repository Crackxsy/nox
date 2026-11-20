"""This machine's own addresses, as the node code needs them on both sides.

The core asks them to tell whether a node it controls is the machine it runs on
(`NodeControl.controls_local`); a node reports the same list in its pong. Owns the one
way to read them and the value that means "could not be read". Does not decide what an
address means (that is `nox.node.hub.world_routes`).
"""

from __future__ import annotations

import psutil

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["ADDRESSES_UNREADABLE", "all_local_addresses"]

#: What `all_local_addresses` returns when this machine's addresses cannot be read: "could be
#: any of them" - while Nox controls a node, this machine's own dashboard and shell then count
#: as the controlled PC (`NodeControl.controls_local`), never as some other machine.
ADDRESSES_UNREADABLE = "*"


def all_local_addresses() -> list[str]:
    """Every address of every interface of this machine - uncapped and unfiltered, loopback,
    virtual adapters and interfaces that are down included: for telling whether a peer is this
    machine (`NodeControl.controls_local`), where a missing address would fail open. When they
    cannot be read: `[ADDRESSES_UNREADABLE]` (fail closed), never an empty list."""

    try:
        interfaces = psutil.net_if_addrs()
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the answer is "unknown"
        log.warning("node.own_addresses_unreadable", error=type(exc).__name__)
        return [ADDRESSES_UNREADABLE]
    found: list[str] = []
    for entries in interfaces.values():
        for entry in entries:
            address = str(entry.address).split("%", 1)[0]
            if address and address not in found:
                found.append(address)
    return found
