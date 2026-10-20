"""Network rules that keep Nox's AppContainers off this computer's loopback, the LAN and the net.

Why they are needed: an AppContainer without capabilities has no internet or LAN access, and
Windows' network isolation is meant to keep it off loopback services of the host too. That last
part is not enforced on every machine - measured on Windows 11 (build 26200, firewall on, no
loopback exemption): a desktop AppContainer connected to any host loopback listener. So the
loopback probe (`nox.sandbox.windows.host_loopback_reachable`) reported "reachable", the
AppContainer did not count as strong, and shell commands needed the weak-sandbox opt-in.

What they are: Windows Filtering Platform (WFP) filters, added by the installer's one elevated
step and removed again on uninstall. That step never runs Nox's own Python: Nox is installed per
user, so its runtime is writable by the user's account, and code from there must never run as an
administrator. The filters are added by a self-contained PowerShell script that ships *inside* the
installer and is pinned by its SHA-256 (`nox.sandbox.wfp_helper`); a copy is kept in an admin-only
folder for uninstall and repair. `python -m nox.sandbox.wfp install|remove` therefore refuses and
names that helper; `show` lists the rules for anyone to check.

Plain WFP rather than Windows Defender Firewall rules, because:

* they sit in a sublayer of Nox's own with the highest weight, and the blocks clear the action
  right ("hard" blocks), so no permit elsewhere - Windows' own loopback handling, a third-party
  firewall's rules - can override them;
* they are enforced by the Base Filtering Engine whether or not Defender Firewall is the active
  firewall (a third-party firewall turns the Defender rules off, not these);
* they match the connecting *package* (the AppContainer SID), so nothing but Nox's containers is
  affected.

An allowlist by construction, per container SID (shell's container and every pool slot,
`nox.sandbox.containers`):

1. at the outbound connect layer (TCP connects and the first UDP send) and the inbound accept
   layer, IPv4 and IPv6: a hard **block of everything**, at a low weight;
2. above it, the one exception, at the highest weight: TCP to `127.0.0.1` on the egress
   forwarder's ports (`FORWARDER_PORTS`) at the connect layer, and the forwarder accepting exactly
   such a connection at the accept layer - the in-container forwarder that relays to Nox's egress
   proxy listens there (`nox.sandbox.windows`).

Nothing is listed as "blocked": there is no list of private ranges to keep complete. IPv4-mapped
and NAT64 addresses, 6to4 (`2002::/16`), Teredo, the host's own LAN and VPN addresses, link-local,
multicast and the public internet are all covered by the block of everything. What the rules do
not do: they cannot tell a listener in the forwarder range that belongs to the host from the
forwarder itself - so the probe refuses "confined" while anything foreign listens in that range,
and every run checks its own forwarder's port (`nox.sandbox.windows`) - and they block a
workload's own loopback outside that range (a skill test that starts a local server and connects
to it). The probe stays the source of truth: the sandbox counts as strong only when it observes
the container being refused.

Everything here is pure Python and runs on any platform (the tests check the exact rules, and that
the helper script derives the very same ones).
"""

from __future__ import annotations

import ctypes
import ipaddress
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from nox.sandbox.containers import all_container_names, container_sid

__all__ = [
    "FORWARDER_PORTS",
    "LAYERS",
    "PROVIDER_KEY",
    "SUBLAYER_KEY",
    "FilterRule",
    "LocalAddress",
    "LocalPortRange",
    "Package",
    "PortRange",
    "Protocol",
    "Remote",
    "all_rules",
    "describe",
    "filter_key",
    "main",
    "rules_for",
    "sid_bytes",
]

#: The WFP provider and sublayer every Nox filter belongs to (fixed: uninstall finds them by key).
PROVIDER_KEY = uuid.UUID("a8885a17-a31d-4472-8a83-5ac136984f68")
SUBLAYER_KEY = uuid.UUID("c7d07ae0-4bf0-4fee-b6b1-61d9fca37220")
#: Namespace of the per-rule filter keys (`filter_key`).
KEY_NAMESPACE = uuid.UUID("ab693b13-a6ef-48c4-8c8c-b27790b328e1")
#: Highest sublayer weight: Nox's sublayer is evaluated first.
SUBLAYER_WEIGHT = 0xFFFF
#: Filter weights inside the sublayer (FWP_UINT8, 0-15): the highest matching filter decides.
PERMIT_WEIGHT = 15
BLOCK_WEIGHT = 1

#: Where the in-container egress forwarder may listen: 128 ports below Windows' dynamic port range
#: (49152-65535 by default), so a host program that asks for "any free port" never lands here.
FORWARDER_PORTS = range(47_100, 47_228)
#: The forwarder's address: the container's own IPv4 loopback.
FORWARDER_ADDRESS = ipaddress.IPv4Network("127.0.0.1/32")
TCP = 6

Layer = Literal[
    "ALE_AUTH_CONNECT_V4",
    "ALE_AUTH_CONNECT_V6",
    "ALE_AUTH_RECV_ACCEPT_V4",
    "ALE_AUTH_RECV_ACCEPT_V6",
]
Action = Literal["PERMIT", "BLOCK"]

#: The WFP layers Nox filters (fwpmu.h). Uninstall looks for Nox's filters in each.
LAYERS: dict[Layer, uuid.UUID] = {
    "ALE_AUTH_CONNECT_V4": uuid.UUID("c38d57d1-05a7-4c33-904f-7fbceee60e82"),
    "ALE_AUTH_CONNECT_V6": uuid.UUID("4a72393b-319f-44bc-84c3-ba54dcb3b6b4"),
    "ALE_AUTH_RECV_ACCEPT_V4": uuid.UUID("e1cd9fe7-f4b5-4273-96c0-592e487b8650"),
    "ALE_AUTH_RECV_ACCEPT_V6": uuid.UUID("a3b42c97-9f04-4672-b87e-cee9c483257f"),
}


@dataclass(frozen=True)
class Package:
    """The connecting process runs in the AppContainer `sid` (FWPM_CONDITION_ALE_PACKAGE_ID)."""

    sid: str


@dataclass(frozen=True)
class Remote:
    """The remote address lies in `network` (FWPM_CONDITION_IP_REMOTE_ADDRESS)."""

    network: ipaddress.IPv4Network


@dataclass(frozen=True)
class PortRange:
    """The remote port lies in [low, high] (FWPM_CONDITION_IP_REMOTE_PORT, FWP_MATCH_RANGE)."""

    low: int
    high: int


@dataclass(frozen=True)
class LocalAddress:
    """The local address lies in `network` (FWPM_CONDITION_IP_LOCAL_ADDRESS)."""

    network: ipaddress.IPv4Network


@dataclass(frozen=True)
class LocalPortRange:
    """The local port lies in [low, high] (FWPM_CONDITION_IP_LOCAL_PORT, FWP_MATCH_RANGE)."""

    low: int
    high: int


@dataclass(frozen=True)
class Protocol:
    """The IP protocol is `number` (FWPM_CONDITION_IP_PROTOCOL)."""

    number: int


Condition = Package | Remote | PortRange | LocalAddress | LocalPortRange | Protocol


@dataclass(frozen=True)
class FilterRule:
    """One WFP filter. Conditions on different fields are AND'ed by WFP."""

    key: uuid.UUID
    name: str
    layer: Layer
    action: Action
    #: Weight inside Nox's sublayer (0-15): the highest matching filter decides.
    weight: int
    #: A hard block: clears the action right, so no lower-priority permit can override it.
    hard: bool
    conditions: tuple[Condition, ...]


#: The rules of one container, in the order the helper script adds them: (rule id, layer, action).
RULE_IDS: tuple[tuple[str, Layer, Action], ...] = (
    ("permit-forwarder-connect-v4", "ALE_AUTH_CONNECT_V4", "PERMIT"),
    ("permit-forwarder-accept-v4", "ALE_AUTH_RECV_ACCEPT_V4", "PERMIT"),
    ("block-all-connect-v4", "ALE_AUTH_CONNECT_V4", "BLOCK"),
    ("block-all-connect-v6", "ALE_AUTH_CONNECT_V6", "BLOCK"),
    ("block-all-accept-v4", "ALE_AUTH_RECV_ACCEPT_V4", "BLOCK"),
    ("block-all-accept-v6", "ALE_AUTH_RECV_ACCEPT_V6", "BLOCK"),
)


def filter_key(container: str, rule: str) -> uuid.UUID:
    """The fixed key of rule `rule` for `container` - the same on every install and every run."""
    return uuid.uuid5(KEY_NAMESPACE, f"{container}|{rule}")


def rules_for(container: str) -> tuple[FilterRule, ...]:
    """The filters for one container, by its name (its SID is derived from the name)."""
    pkg = Package(container_sid(container))
    low, high = FORWARDER_PORTS.start, FORWARDER_PORTS.stop - 1
    extra: dict[str, tuple[Condition, ...]] = {
        "permit-forwarder-connect-v4": (
            Protocol(TCP),
            Remote(FORWARDER_ADDRESS),
            PortRange(low, high),
        ),
        "permit-forwarder-accept-v4": (
            Protocol(TCP),
            LocalAddress(FORWARDER_ADDRESS),
            LocalPortRange(low, high),
        ),
    }
    return tuple(
        FilterRule(
            key=filter_key(container, rule_id),
            name=f"Nox sandbox {container}: {rule_id}",
            layer=layer,
            action=action,
            weight=PERMIT_WEIGHT if action == "PERMIT" else BLOCK_WEIGHT,
            hard=action == "BLOCK",
            conditions=(pkg, *extra.get(rule_id, ())),
        )
        for rule_id, layer, action in RULE_IDS
    )


def all_rules(containers: Sequence[str] | None = None) -> tuple[FilterRule, ...]:
    """Every filter Nox installs: the rules of every container it can ever run a workload in."""
    names = all_container_names() if containers is None else tuple(containers)
    return tuple(r for name in names for r in rules_for(name))


def sid_bytes(sid: str) -> bytes:
    """The binary form of a SID string (what WFP's FWP_SID condition points at)."""
    parts = sid.split("-")
    if len(parts) < 3 or parts[0] != "S" or parts[1] != "1":
        raise ValueError(f"not a SID: {sid!r}")
    authority = int(parts[2])
    subs = [int(p) for p in parts[3:]]
    if not 0 <= authority < 2**48 or len(subs) > 15 or any(not 0 <= s < 2**32 for s in subs):
        raise ValueError(f"not a SID: {sid!r}")
    out = bytes([1, len(subs)]) + authority.to_bytes(6, "big")
    return out + b"".join(s.to_bytes(4, "little") for s in subs)


def _describe_condition(cond: Condition) -> str:
    match cond:
        case Package(sid):
            return f"package={sid}"
        case Remote(network):
            return f"remote={network}"
        case PortRange(low, high):
            return f"remote-port={low}-{high}"
        case LocalAddress(network):
            return f"local={network}"
        case LocalPortRange(low, high):
            return f"local-port={low}-{high}"
        case Protocol(number):
            return f"protocol={number}"
    raise TypeError(cond)  # pragma: no cover - the union is exhaustive


def describe(rules: Sequence[FilterRule]) -> list[str]:
    """One line per filter - what `nox sandbox firewall show` and the helper's `show` print."""
    return [
        f"{r.key} {r.layer} {r.action}{' (hard)' if r.hard else ''} w={r.weight} "
        + " ".join(_describe_condition(c) for c in r.conditions)
        for r in rules
    ]


def is_elevated() -> bool:
    """Whether this process runs with an administrator token (Windows only)."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined,unused-ignore]
    except (AttributeError, OSError):
        return False


_USAGE = "usage: python -m nox.sandbox.wfp {install|remove|show}"
#: Exit code of `install`/`remove`: Nox's own Python never changes the rules itself.
REFUSED = 4


def refusal(action: str, *, elevated: bool) -> str:
    """Why `install`/`remove` refuse here, and the protected helper to use instead."""
    from nox.sandbox.wfp_helper import admin_command

    why = (
        "refusing to run as administrator from Nox's own Python runtime: it lives in a folder "
        "your user account can change, and code from there never runs elevated."
        if elevated
        else "changing them needs an administrator, and Nox's own Python never runs as one "
        "(its folder is writable by your user account)."
    )
    return (
        f"sandbox network rules: {why}\n"
        "Use the protected helper instead: run the Nox setup again with the sandbox rules ticked "
        "(it keeps the helper in an admin-only folder), or, once it is there, run from an "
        f"administrator prompt:\n  {admin_command(action)}"
    )


def _run(action: str) -> tuple[int, str]:
    if action == "show":
        return 0, "\n".join(describe(all_rules()))
    if sys.platform != "win32":
        return 3, "the sandbox network rules exist on Windows only"
    return REFUSED, refusal(action, elevated=is_elevated())


def main(argv: Sequence[str] | None = None) -> int:
    """`show` (anyone) lists every rule; `install` / `remove` refuse and name the protected helper.

    Exit codes: 0 done, 2 usage, 3 not Windows, 4 refused (the message says what to run instead).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("install", "remove", "show"):
        print(_USAGE, file=sys.stderr)  # noqa: T201 - a command-line entry point
        return 2
    code, message = _run(args[0])
    print(message, file=sys.stderr if code else sys.stdout)  # noqa: T201
    return code


if __name__ == "__main__":
    raise SystemExit(main())
