"""Network rules that keep Nox's AppContainers off this computer's loopback and the LAN.

Why they are needed: an AppContainer without capabilities has no internet or LAN access, and
Windows' network isolation is meant to keep it off loopback services of the host too. That last
part is not enforced on every machine - measured on Windows 11 (build 26200, firewall on, no
loopback exemption): a desktop AppContainer connected to any host loopback listener. So the
loopback probe (`nox.sandbox.windows.host_loopback_reachable`) reported "reachable", the
AppContainer did not count as strong, and shell commands needed the weak-sandbox opt-in.

What they are: Windows Filtering Platform (WFP) filters, added once by the installer's elevated
step (`python -m nox.sandbox.wfp install`, which needs an administrator) and removed again on
uninstall. Plain WFP rather than Windows Defender Firewall rules, because:

* they sit in a sublayer of Nox's own with the highest weight, and the blocks clear the action
  right ("hard" blocks), so no permit elsewhere - Windows' own loopback handling included - can
  override them; Defender Firewall rules live in the firewall's sublayer, whose built-in loopback
  handling decides first;
* they are enforced by the Base Filtering Engine whether or not Defender Firewall is the active
  firewall (a third-party firewall turns the Defender rules off, not these);
* they can match the "is loopback" flag of a connection, which also covers this computer's own
  LAN and VPN addresses, not just `127.0.0.0/8`.

Per container SID (shell's container and every pool slot, `nox.sandbox.containers`), at the
outbound connect layer (TCP connects and the first UDP send), IPv4 and IPv6:

1. permit TCP to `127.0.0.1` on the egress forwarder's ports (`FORWARDER_PORTS`) - the in-container
   forwarder that relays to Nox's egress proxy listens there (`nox.sandbox.windows`);
2. block every local, private, link-local, CGNAT, multicast and reserved destination
   (`BLOCKED_V4`, `BLOCKED_V6`);
3. block every connection WFP flags as loopback.

Rule 1 has the highest weight in the sublayer, so it wins over 2 and 3 for exactly that address and
port range; everything else local is blocked. What the rules do not do: they cannot tell a
listener in the forwarder range that belongs to the host from the forwarder itself - so the probe
also refuses "confined" while anything on the host listens in that range - and they block a
workload's own loopback outside that range (a skill test that starts a local server and connects to
it). The probe stays the source of truth: the sandbox counts as strong only when it observes the
container being refused.

Everything here is pure Python and runs on any platform (the tests check the exact rules); only
`apply`/`remove` talk to Windows (`nox.sandbox._wfp_win32`).
"""

from __future__ import annotations

import ipaddress
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from nox.sandbox.containers import all_container_names, container_sid

__all__ = [
    "BLOCKED_V4",
    "BLOCKED_V6",
    "FORWARDER_PORTS",
    "PROVIDER_KEY",
    "SUBLAYER_KEY",
    "FilterRule",
    "Flags",
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
_KEY_NAMESPACE = uuid.UUID("ab693b13-a6ef-48c4-8c8c-b27790b328e1")
#: Highest sublayer weight: Nox's sublayer is evaluated first.
SUBLAYER_WEIGHT = 0xFFFF

#: Where the in-container egress forwarder may listen: 128 ports below Windows' dynamic port range
#: (49152-65535 by default), so a host program that asks for "any free port" never lands here.
FORWARDER_PORTS = range(47_100, 47_228)

#: IPv4 destinations a contained workload may never reach directly.
BLOCKED_V4: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.IPv4Network(net)
    for net in (
        "0.0.0.0/8",  # "this network": reaches the host itself on some stacks
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT, Tailscale
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, cloud metadata
        "172.16.0.0/12",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",  # multicast (LAN discovery)
        "240.0.0.0/4",  # reserved, broadcast
    )
)
#: IPv6 destinations a contained workload may never reach directly.
BLOCKED_V6: tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.IPv6Network(net)
    for net in (
        "::/127",  # unspecified and loopback (::1)
        "::ffff:0:0/96",  # IPv4-mapped
        "64:ff9b::/96",  # NAT64: embeds any IPv4 address, private ones included
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (deprecated, still routable on some LANs)
        "ff00::/8",  # multicast
    )
)

#: FWP_CONDITION_FLAG_IS_LOOPBACK: WFP sets it on every connection that stays on this computer.
FLAG_IS_LOOPBACK = 0x00000001
TCP = 6

Layer = Literal["ALE_AUTH_CONNECT_V4", "ALE_AUTH_CONNECT_V6"]
Action = Literal["PERMIT", "BLOCK"]


@dataclass(frozen=True)
class Package:
    """The connecting process runs in the AppContainer `sid` (FWPM_CONDITION_ALE_PACKAGE_ID)."""

    sid: str


@dataclass(frozen=True)
class Remote:
    """The destination address lies in `network` (FWPM_CONDITION_IP_REMOTE_ADDRESS)."""

    network: ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class PortRange:
    """The destination port lies in [low, high] (FWPM_CONDITION_IP_REMOTE_PORT, FWP_MATCH_RANGE)."""

    low: int
    high: int


@dataclass(frozen=True)
class Protocol:
    """The IP protocol is `number` (FWPM_CONDITION_IP_PROTOCOL)."""

    number: int


@dataclass(frozen=True)
class Flags:
    """Any of `mask` is set in the connection's flags (FWPM_CONDITION_FLAGS, FLAGS_ANY_SET)."""

    mask: int


Condition = Package | Remote | PortRange | Protocol | Flags


@dataclass(frozen=True)
class FilterRule:
    """One WFP filter. Conditions on the same field are OR'ed by WFP, different fields AND'ed."""

    key: uuid.UUID
    name: str
    layer: Layer
    action: Action
    #: Weight inside Nox's sublayer (0-15): the highest matching filter decides.
    weight: int
    #: A hard block: clears the action right, so no lower-priority permit can override it.
    hard: bool
    conditions: tuple[Condition, ...]


def filter_key(container: str, rule: str) -> uuid.UUID:
    """The fixed key of rule `rule` for `container` - the same on every install and every run."""
    return uuid.uuid5(_KEY_NAMESPACE, f"{container}|{rule}")


def rules_for(container: str) -> tuple[FilterRule, ...]:
    """The filters for one container, by its name (its SID is derived from the name)."""
    sid = container_sid(container)
    pkg = Package(sid)

    def rule(
        rule_id: str, layer: Layer, action: Action, weight: int, *conds: Condition
    ) -> FilterRule:
        return FilterRule(
            key=filter_key(container, rule_id),
            name=f"Nox sandbox {container}: {rule_id}",
            layer=layer,
            action=action,
            weight=weight,
            hard=action == "BLOCK",
            conditions=(pkg, *conds),
        )

    return (
        rule(
            "permit-egress-forwarder",
            "ALE_AUTH_CONNECT_V4",
            "PERMIT",
            15,
            Protocol(TCP),
            Remote(ipaddress.IPv4Network("127.0.0.1/32")),
            PortRange(FORWARDER_PORTS.start, FORWARDER_PORTS.stop - 1),
        ),
        rule(
            "block-local-v4",
            "ALE_AUTH_CONNECT_V4",
            "BLOCK",
            10,
            *(Remote(net) for net in BLOCKED_V4),
        ),
        rule("block-loopback-v4", "ALE_AUTH_CONNECT_V4", "BLOCK", 9, Flags(FLAG_IS_LOOPBACK)),
        rule(
            "block-local-v6",
            "ALE_AUTH_CONNECT_V6",
            "BLOCK",
            10,
            *(Remote(net) for net in BLOCKED_V6),
        ),
        rule("block-loopback-v6", "ALE_AUTH_CONNECT_V6", "BLOCK", 9, Flags(FLAG_IS_LOOPBACK)),
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
        case Protocol(number):
            return f"protocol={number}"
        case Flags(mask):
            return f"flags-any={mask:#x}"
    raise TypeError(cond)  # pragma: no cover - the union is exhaustive


def describe(rules: Sequence[FilterRule]) -> list[str]:
    """One line per filter - what `nox sandbox firewall show` prints."""
    return [
        f"{r.key} {r.layer} {r.action}{' (hard)' if r.hard else ''} w={r.weight} "
        + " ".join(_describe_condition(c) for c in r.conditions)
        for r in rules
    ]


def apply() -> int:
    """Install every filter (idempotent: each one is replaced). Needs an administrator."""
    from nox.sandbox import _wfp_win32

    return _wfp_win32.install(all_rules())


def remove() -> int:
    """Remove every filter of Nox's provider, its sublayer and the provider itself."""
    from nox.sandbox import _wfp_win32

    return _wfp_win32.uninstall(all_rules())


_USAGE = "usage: python -m nox.sandbox.wfp {install|remove|show} [--status-file PATH]"


def _run(action: str) -> tuple[int, str]:
    if action == "show":
        return 0, "\n".join(describe(all_rules()))
    if sys.platform != "win32":
        return 3, "the sandbox network rules exist on Windows only"
    try:
        count = apply() if action == "install" else remove()
    except OSError as exc:
        return 1, f"sandbox network rules: {exc}"
    verb = "installed" if action == "install" else "removed"
    return 0, f"sandbox network rules: {verb} {count} filter(s)"


def main(argv: Sequence[str] | None = None) -> int:
    """The installer's entry point: `install` / `remove` (elevated), `show` (anyone).

    Exit codes: 0 done, 1 Windows refused (the message says why), 2 usage, 3 not Windows. With
    `--status-file`, the exit code and the message are also written there (first line, rest) -
    the installer starts this step elevated through the shell, which hands it no exit code.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    status_file: str | None = None
    if len(args) == 3 and args[1] == "--status-file":
        status_file = args.pop(2)
        args.pop(1)
    if len(args) != 1 or args[0] not in ("install", "remove", "show"):
        print(_USAGE, file=sys.stderr)  # noqa: T201 - a command-line entry point
        return 2
    code, message = _run(args[0])
    print(message, file=sys.stderr if code else sys.stdout)  # noqa: T201
    if status_file is not None:
        try:
            with open(status_file, "w", encoding="utf-8") as fh:
                fh.write(f"{code}\n{message}\n")
        except OSError as exc:
            print(f"could not write {status_file}: {exc}", file=sys.stderr)  # noqa: T201
    return code


if __name__ == "__main__":
    raise SystemExit(main())
