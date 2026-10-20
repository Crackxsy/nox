"""Deciding whether a destination IP is one the sandbox (and `web.fetch`) must never reach.

One place owns the rule "this address is off-limits", because two callers need it and they must
agree: the sandbox egress proxy (what a sandboxed process may connect to) and `web.fetch` (what a
URL may resolve to). Both resolve the host first and judge the *address*, so a name that points at
`127.0.0.1`, a `169.254` metadata address or the LAN is refused however it is spelt - which is the
whole defence against DNS rebinding.

Forbidden unless explicitly declared and confirmed:

* loopback (`127/8`, `::1`), link-local (`169.254/16`, `fe80::/10`) and the cloud metadata address
  `169.254.169.254`,
* every private range (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`) - the home LAN - and
  anything else that is not globally routable (CGNAT/Tailscale `100.64/10` included), also when
  it hides inside an IPv6 transition address (mapped, 6to4, Teredo, NAT64),
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


#: NAT64 well-known prefix (RFC 6052): `64:ff9b::a.b.c.d` reaches IPv4 `a.b.c.d` through a
#: translator, and Python calls the prefix global - so the embedded address must be judged.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
#: IPv4-compatible IPv6 (deprecated, RFC 4291 2.5.5.1): `::a.b.c.d`. Python calls it global.
_V4_COMPATIBLE = ipaddress.IPv6Network("::/96")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries inside it (mapped, 6to4, Teredo, NAT64,
    IPv4-compatible), or None. Each of these can route to the embedded v4 host, so a private v4
    must not be smuggled through any of them."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]  # (server, client): the client is the host the packet reaches
    if ip in _NAT64 or ip in _V4_COMPATIBLE:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """A reason this address is off-limits, or None when it is an ordinary public address.

    The rule is an allow-list, not a deny-list: anything that is not globally routable is refused
    (`not ip.is_global`), which covers CGNAT/Tailscale `100.64/10`, benchmarking `198.18/15`, the
    IETF protocol block and every range IANA adds later. The specific checks before it only give a
    precise reason; the ones after it close what Python still calls "global" but is not a plain
    public unicast host (multicast, site-local, and IPv6 forms that embed an IPv4 address).
    """
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address (includes cloud metadata)"
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            inner = _is_forbidden_ip(embedded)
            if inner is not None:
                return f"IPv6 address embedding a forbidden IPv4 ({inner})"
            # A public v4 behind a transition prefix: judge the v6 wrapper no further, the
            # translator reaches exactly that public host.
            return None
        if ip.is_site_local:
            return "site-local address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return "shared address space (CGNAT / Tailscale)"
    if ip.is_private:
        return "private / LAN address"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "reserved or non-unicast address"
    if not ip.is_global:
        return "not a globally routable address"
    return None


_CGNAT = ipaddress.IPv4Network("100.64.0.0/10")


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
