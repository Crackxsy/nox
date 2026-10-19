"""Deciding whether a destination IP is one the sandbox (and `web.fetch`) must never reach.

One place owns the rule "this address is off-limits", because two callers need it and they must
agree: the sandbox egress proxy (what a sandboxed process may connect to) and `web.fetch` (what a
URL may resolve to). Both resolve the host first and judge the *address*, so a name that points at
`127.0.0.1`, a `169.254` metadata address or the LAN is refused however it is spelt - which is the
whole defence against DNS rebinding.

Forbidden unless explicitly declared and confirmed:

* loopback (`127/8`, `::1`), link-local (`169.254/16`, `fe80::/10`) and the cloud metadata address
  `169.254.169.254`,
* every private range (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`) - the home LAN,
* the configured Home Assistant host, resolved to its address, always, with no override here (the
  home boundary lives below this and is not negotiable).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["BlockedAddress", "NetGuard", "resolve_all"]


class BlockedAddress(PermissionError):  # noqa: N818 - parallels EgressDenied
    def __init__(self, host: str, reason: str) -> None:
        super().__init__(f"{host}: {reason}")
        self.host = host
        self.reason = reason


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """A reason this address is off-limits, or None when it is an ordinary public address."""
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address (includes cloud metadata)"
    if ip.is_private:
        return "private / LAN address"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "reserved or non-unicast address"
    # Map IPv4-in-IPv6 (::ffff:10.0.0.1) back and re-judge, so it cannot smuggle a private v4 in.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _is_forbidden_ip(mapped)
    return None


def resolve_all(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address `host` resolves to right now. A bare IP resolves to itself."""
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        addr = info[4][0]
        try:
            out.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return out


@dataclass(frozen=True, slots=True)
class NetGuard:
    """Judges a destination by its resolved address(es). Pure given a resolver."""

    #: Addresses the configured Home Assistant host resolves to; never reachable from here.
    home_assistant_ips: frozenset[str] = frozenset()
    #: Extra hostnames that are always blocked by name (belt and braces alongside the IP check).
    blocked_hosts: frozenset[str] = frozenset()

    def check_addresses(
        self, host: str, addresses: Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address]
    ) -> None:
        """Raise `BlockedAddress` if any resolved address is off-limits. Empty list raises too."""
        if host.strip().lower().rstrip(".") in self.blocked_hosts:
            raise BlockedAddress(host, "explicitly blocked host")
        if not addresses:
            raise BlockedAddress(host, "does not resolve to any address")
        for ip in addresses:
            reason = _is_forbidden_ip(ip)
            if reason is not None:
                raise BlockedAddress(host, reason)
            if str(ip) in self.home_assistant_ips:
                raise BlockedAddress(host, "the Home Assistant host is never reachable this way")

    def check(self, host: str, port: int) -> None:
        """Resolve `host` and judge every address it points at."""
        self.check_addresses(host, resolve_all(host, port))
