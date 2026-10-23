"""Nox-OS firewall: the nftables rules, generated from Nox's own configuration (os/README.md).

    python3 -I nox_firewall.py apply      read the configuration, load the rules (root)
    python3 -I nox_firewall.py baseline   print the rules with no Nox port open (build time)
    python3 -I nox_firewall.py print      print what `apply` would load
    python3 -I nox_firewall.py sample [public]   every switch on (CI checks it with `nft -c`)

Inbound is default-deny, IPv4 and IPv6, in one table of its own (`inet nox_os`), replaced
atomically. Open: loopback, answers to connections this device made, the ICMP that IPv4 and IPv6
need, the DHCP client, mDNS (`nox.local`), and - only while network access is on
(`ipc.lan.enabled`) - Nox's dashboard and node ports, from private networks only unless
`ipc.lan.allow_public_clients` says otherwise, with new connections rate-limited per address.
While the device has no owner yet (no root-owned /var/lib/nox-os-claim/claimed, step O3) only
the dashboard port is opened - Nox serves nothing there but its claim page then - and the node
port stays closed. SSH (port 22) only while remote maintenance is on (/run/nox-os/ssh.json,
written by the root helper), from private networks only. The setup ports (HTTP, DNS, DHCP server)
only in setup mode (/run/nox-os/setup-mode, its end not reached) and only on the setup hotspot's
interface (/run/nox-os/setup/hotspot.json). That interface gets nothing else - neither Nox's
ports nor SSH - for as long as the hotspot exists, window open or not: its isolation never waits
for the hotspot to be taken down.
Outbound is open - Nox needs the internet - except the cloud metadata addresses.

The ports come from `config/defaults.yaml` of the installed release and `/var/lib/nox/user.yaml`,
so they follow the configuration instead of drifting from it. A port this script would not open
(below 1024, or one of the system's own) is refused, whatever the configuration says: the
configuration file belongs to the service user, and the service user must not be able to open
SSH or a privileged port. When anything fails, the rules loaded at boot (`baseline`, no Nox port
open) stay in force: the firewall fails closed.

Standard library plus PyYAML - the distribution's (python3-yaml), run by the system interpreter:
root never runs code from Nox's own environment, which Nox updates itself.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Where the settings live on Nox-OS (scripts/install-linux.sh).
DEFAULTS = Path("/opt/nox/current/config/defaults.yaml")
USER = Path("/var/lib/nox/user.yaml")
#: Written by the root helper (nox_os_helper.py) while remote maintenance is on.
SSH_STATE = Path("/run/nox-os/ssh.json")
#: Step O3 creates it while the device is in setup mode (hotspot and captive portal).
SETUP_MODE = Path("/run/nox-os/setup-mode")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
#: The setup hotspot's interface while it is up (nox_setup.py).
HOTSPOT = Path("/run/nox-os/setup/hotspot.json")
HOTSPOT_NET = "10.42.0.0/24"
#: Root's mark that the device has an owner (the root helper writes it at the claim).
CLAIMED = Path("/var/lib/nox-os-claim/claimed")
#: What the console status screen reads (no secrets: ports and switches).
SUMMARY = Path("/run/nox-os/firewall.json")
NFT = "/usr/sbin/nft"
TABLE = "nox_os"

#: Used only when the release's defaults cannot be read (the values in config/defaults.yaml).
DEFAULT_PORT = 47843
DEFAULT_NODE_PORT = 47844
FALLBACK: dict[str, Any] = {
    "enabled": False,
    "port": DEFAULT_PORT,
    "node_port": DEFAULT_NODE_PORT,
    "allow_public_clients": False,
}
#: The private networks Nox's own network listener answers (`nox.lan.tls.PERMITTED_NETWORKS`
#: without loopback; tests/unit/nox_os/test_firewall.py keeps the two in step).
PRIVATE_V4 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
PRIVATE_V6 = ("fc00::/7", "fe80::/10")
#: Never opened from configuration: SSH has its own switch, the setup ports their own mode, and
#: the core's loopback ports (47800, 47801) must never be reachable from outside.
RESERVED_PORTS = frozenset({22, 53, 67, 68, 80, 546, 547, 5353, 47800, 47801})
SSH_PORT = 22
#: New connections per source address: a phone opening the dashboard makes a handful at once.
DASHBOARD_RATE = "60/minute burst 40 packets"
SSH_RATE = "10/minute burst 5 packets"
#: Cloud metadata services: nothing on a home device has a reason to reach them.
METADATA_V4 = "169.254.169.254"
METADATA_V6 = "fd00:ec2::254"
MAX_CONFIG_BYTES = 1 << 20


@dataclass(frozen=True)
class Settings:
    lan_enabled: bool = False
    port: int = DEFAULT_PORT
    node_port: int = DEFAULT_NODE_PORT
    public: bool = False
    ssh: bool = False
    setup: bool = False
    #: The device has an owner: the node port may open. Before, only the dashboard (claim page).
    claimed: bool = False
    #: The setup hotspot's interface while it exists - isolated always, its setup ports open only
    #: while the setup window is.
    hotspot: str | None = None


def _read_yaml(path: Path) -> dict[str, Any]:
    """A YAML mapping, or {} for a file that is missing, a link, not a regular file, too large or
    not a mapping. Opened without following a link: the user file belongs to the service user."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            return {}
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    try:
        import yaml  # noqa: PLC0415 - only `apply`/`print` need it; `baseline` runs without

        data = yaml.safe_load(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable layer counts as absent, like Nox's loader
        return {}
    return data if isinstance(data, dict) else {}


def _lan_section(layer: Mapping[str, Any]) -> dict[str, Any]:
    ipc = layer.get("ipc")
    lan = ipc.get("lan") if isinstance(ipc, dict) else None
    return dict(lan) if isinstance(lan, dict) else {}


def _port(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def openable(port: int) -> bool:
    """Whether a port from the configuration may be opened at all."""
    return 1024 <= port <= 65535 and port not in RESERVED_PORTS


def settings_from(
    defaults: Mapping[str, Any],
    user: Mapping[str, Any],
    *,
    ssh: bool,
    setup: bool,
    claimed: bool = False,
    hotspot: str | None = None,
) -> Settings:
    """The firewall's view of the configuration: the user layer over the defaults, ipc.lan only."""
    lan = {**FALLBACK, **_lan_section(defaults)}
    lan.update(_lan_section(user))
    enabled = lan.get("enabled") is True
    return Settings(
        lan_enabled=enabled,
        port=_port(lan.get("port"), DEFAULT_PORT),
        node_port=_port(lan.get("node_port"), DEFAULT_NODE_PORT),
        public=lan.get("allow_public_clients") is True,
        ssh=ssh,
        setup=setup,
        claimed=claimed,
        hotspot=hotspot if hotspot and _IFACE.fullmatch(hotspot) else None,
    )


_IFACE = re.compile(r"[a-z][a-z0-9]{0,14}")


def _root_file(path: Path, owner: int = 0) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == owner


def _boottime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)  # type: ignore[attr-defined,unused-ignore]


def setup_window_open(
    path: Path = SETUP_MODE, boot_id: Path = BOOT_ID, uptime: float | None = None, owner: int = 0
) -> bool:
    """Root's setup-mode file, this boot's, its end (seconds since boot) not reached - the same
    rule as the root helper and nox_setup.py. An expired window opens nothing, even before it
    is removed."""
    if not _root_file(path, owner):
        return False
    try:
        mode = json.loads(path.read_text("utf-8"))
        current = boot_id.read_text("ascii").strip()
    except (OSError, ValueError):
        return False
    if not isinstance(mode, dict):
        return False
    until = mode.get("until")
    now = _boottime() if uptime is None else uptime
    return (
        mode.get("v") == 1
        and bool(current)
        and mode.get("boot_id") == current
        and not isinstance(until, bool)
        and isinstance(until, (int, float))
        and now < until
    )


def hotspot_iface(path: Path = HOTSPOT, owner: int = 0) -> str | None:
    if not _root_file(path, owner):
        return None
    try:
        state = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    iface = state.get("iface") if isinstance(state, dict) else None
    return iface if isinstance(iface, str) and _IFACE.fullmatch(iface) else None


def ssh_on(path: Path = SSH_STATE, now: float | None = None) -> bool:
    """Remote maintenance is on and its time is not up (a stale file opens nothing)."""
    try:
        if path.is_symlink() or path.stat().st_uid != 0:
            return False
        state = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict) or state.get("on") is not True:
        return False
    until = state.get("until")
    current = time.time() if now is None else now
    return isinstance(until, (int, float)) and not isinstance(until, bool) and until > current


#: Per address family: (version, source address expression, the private networks' set).
_FAMILIES = (("4", "ip saddr", "@private4"), ("6", "ip6 saddr", "@private6"))


def _private(rule: str) -> list[str]:
    """`rule` for IPv4 and IPv6, from private networks only."""
    return [f"{saddr} {private} {rule}" for _v, saddr, private in _FAMILIES]


def _limited(port: int, rate: str, private_only: bool) -> list[str]:
    """New connections to `port`, per address family: over `rate` per source address dropped,
    the rest accepted - from private networks only when `private_only`."""
    lines = []
    for version, saddr, private in _FAMILIES:
        source = f"{saddr} {private}" if private_only else f"meta nfproto ipv{version}"
        lines.append(
            f"{source} tcp dport {port} ct state new "
            f"add @rate{version}_{port} {{ {saddr} limit rate over {rate} }} drop"
        )
        lines.append(f"{source} tcp dport {port} ct state new accept")
    return lines


def render(settings: Settings, *, boot: bool = False) -> str:
    """The complete nftables script: replaces the table atomically and touches no other table -
    except at boot (`boot`, /etc/nftables.conf), where it starts from an empty ruleset."""
    opened: list[int] = []
    # Unclaimed: the dashboard port only (Nox serves its claim page there), never the node port.
    ports = (settings.port, settings.node_port) if settings.claimed else (settings.port,)
    for port in ports:
        if settings.lan_enabled and openable(port) and port not in opened:
            opened.append(port)
    private = ", ".join(PRIVATE_V4)
    private6 = ", ".join(PRIVATE_V6)
    sets = [
        f"  set private4 {{ type ipv4_addr; flags interval; elements = {{ {private} }} }}",
        f"  set private6 {{ type ipv6_addr; flags interval; elements = {{ {private6} }} }}",
    ]
    rate_ports = [*opened, *([SSH_PORT] if settings.ssh else [])]
    for port in rate_ports:
        sets.append(
            f"  set rate4_{port} {{ type ipv4_addr; size 4096; flags dynamic,timeout; timeout 5m; }}"
        )
        sets.append(
            f"  set rate6_{port} {{ type ipv6_addr; size 4096; flags dynamic,timeout; timeout 5m; }}"
        )
    rules = [
        # SSH never from this device itself (a connection to its own address runs over "lo"): a
        # local process must not reach nox-admin's root through remote maintenance.
        f'iif "lo" tcp dport {SSH_PORT} drop',
        'iif "lo" accept',
        "ct state invalid drop",
        "ct state established,related accept",
        # ICMP that IPv4 and IPv6 need; ping answered, at a bounded rate.
        "icmp type { destination-unreachable, time-exceeded, parameter-problem } accept",
        "icmp type echo-request limit rate 10/second accept",
        "icmpv6 type { destination-unreachable, packet-too-big, time-exceeded, parameter-problem, "
        "nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert, mld-listener-query, "
        "mld2-listener-report } accept",
        "icmpv6 type echo-request limit rate 10/second accept",
        # Multicast membership queries, so switches keep forwarding mDNS to this device.
        "ip saddr @private4 ip protocol igmp accept",
        # The DHCP client's answers (not a connection the kernel tracks), DHCPv6 from the router.
        "udp sport 67 udp dport 68 accept",
        "ip6 saddr fe80::/10 udp sport 547 udp dport 546 accept",
    ]
    if settings.hotspot:
        iface = f'iifname "{settings.hotspot}"'
        rules.append("# Setup hotspot (O3): the portal, DHCP and DNS in setup mode - nothing else")
        if settings.setup:
            rules += [
                f"{iface} ip saddr {HOTSPOT_NET} tcp dport 80 accept",
                f"{iface} udp sport 68 udp dport 67 accept",
                f"{iface} ip saddr {HOTSPOT_NET} meta l4proto {{ tcp, udp }} th dport 53 accept",
            ]
        rules.append(f"{iface} drop")
    # mDNS: `nox.local` and finding devices in the home network.
    rules += _private("udp dport 5353 accept")
    for port in opened:
        rules.append(f"# Nox: {'dashboard' if port == settings.port else 'nodes'} (ipc.lan)")
        rules.extend(_limited(port, DASHBOARD_RATE, not settings.public))
    if settings.ssh:
        rules.append("# Remote maintenance: on, key only, from the home network")
        rules.extend(_limited(SSH_PORT, SSH_RATE, True))
    output = [
        f"ip daddr {METADATA_V4} drop",
        f"ip6 daddr {METADATA_V6} drop",
    ]

    def chain(name: str, hook: str, policy: str, body: list[str]) -> list[str]:
        lines = [
            f"  chain {name} {{",
            f"    type filter hook {hook} priority filter; policy {policy};",
        ]
        lines += [f"    {line}" for line in body]
        lines.append("  }")
        return lines

    script = [
        "#!/usr/sbin/nft -f",
        "# Nox-OS firewall - generated by /usr/local/lib/nox-os/nox_firewall.py from Nox's",
        "# configuration. Do not edit: it is replaced at every boot and every change.",
        *(["flush ruleset"] if boot else []),
        f"table inet {TABLE}",
        f"delete table inet {TABLE}",
        f"table inet {TABLE} {{",
        *sets,
        *chain("input", "input", "drop", rules),
        *chain("forward", "forward", "drop", []),
        *chain("output", "output", "accept", output),
        "}",
    ]
    return "\n".join(script) + "\n"


def summary(settings: Settings) -> dict[str, Any]:
    """What the console status screen shows: which switch opened which port. No secrets."""
    return {
        "lan_enabled": settings.lan_enabled,
        "dashboard_port": settings.port
        if settings.lan_enabled and openable(settings.port)
        else None,
        "node_port": (
            settings.node_port if settings.lan_enabled and openable(settings.node_port) else None
        ),
        "ssh": settings.ssh,
        "setup": settings.setup,
        "hotspot": settings.hotspot,
        "claimed": settings.claimed,
        "public": settings.public,
        "updated": int(time.time()),
    }


def current_settings() -> Settings:
    setup = setup_window_open()
    return settings_from(
        _read_yaml(DEFAULTS),
        _read_yaml(USER),
        ssh=ssh_on(),
        setup=setup,
        claimed=_root_file(CLAIMED),
        hotspot=hotspot_iface(),
    )


def _write_summary(settings: Settings) -> None:
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUMMARY.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary(settings)) + "\n", "utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, SUMMARY)


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) == 2 else ""
    if command == "baseline":
        sys.stdout.write(render(Settings(), boot=True))
        return 0
    if argv[1:2] == ["sample"] and argv[2:] in ([], ["public"]):
        public = argv[2:] == ["public"]
        sample = Settings(
            lan_enabled=True, public=public, ssh=True, setup=True, claimed=True, hotspot="wlan0"
        )
        sys.stdout.write(render(sample))
        return 0
    if command == "print":
        sys.stdout.write(render(current_settings()))
        return 0
    if command != "apply":
        sys.stderr.write("usage: nox_firewall.py apply | print | baseline | sample [public]\n")
        return 2
    settings = current_settings()
    loaded = subprocess.run(  # noqa: S603 - fixed program, rules from render() on stdin
        [NFT, "-f", "-"], input=render(settings), text=True, capture_output=True, check=False
    )
    if loaded.returncode != 0:
        sys.stderr.write(f"nft refused the rules, the previous ones stay: {loaded.stderr}\n")
        return 1
    _write_summary(settings)
    opened = summary(settings)
    print(
        "nox-firewall: default deny; open:",
        ", ".join(
            f"{name} {port}"
            for name, port in (
                ("dashboard", opened["dashboard_port"]),
                ("nodes", opened["node_port"]),
                ("ssh", SSH_PORT if settings.ssh else None),
            )
            if port
        )
        or "nothing of Nox's",
        "(setup mode)" if settings.setup else "",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
