"""`NetGuard`: every off-limits address class is refused, by resolved address, on every spelling.

The DNS-rebinding test is the important negative one: a perfectly ordinary name is given, but the
resolver returns a private address, and the guard must still refuse - because it judges the
address, not the name.
"""

from __future__ import annotations

import ipaddress

import pytest

from nox.sandbox.netguard import BlockedAddress, NetGuard


def _ips(*addrs: str) -> list:
    return [ipaddress.ip_address(a) for a in addrs]


@pytest.mark.parametrize(
    "addr,reason",
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("10.0.0.5", "private"),
        ("192.168.1.1", "private"),
        ("172.16.9.9", "private"),
        ("169.254.169.254", "link-local"),
        ("169.254.1.1", "link-local"),
    ],
)
def test_private_and_metadata_addresses_blocked(addr: str, reason: str) -> None:
    guard = NetGuard()
    # The exact reason matters: each address class has its own check, not just "is_private".
    with pytest.raises(BlockedAddress, match=reason):
        guard.check_addresses("whatever.test", _ips(addr))


def test_public_address_allowed() -> None:
    NetGuard().check_addresses("example.com", _ips("93.184.216.34"))  # no raise


def test_dns_rebinding_blocked_by_resolved_address() -> None:
    # The name looks innocent; it resolves to the LAN. The guard judges the address.
    guard = NetGuard()
    with pytest.raises(BlockedAddress):
        guard.check_addresses("totally-normal.example", _ips("192.168.0.50"))


def test_any_private_address_among_several_blocks() -> None:
    guard = NetGuard()
    with pytest.raises(BlockedAddress):
        guard.check_addresses("mixed.example", _ips("93.184.216.34", "10.0.0.1"))


def test_ipv4_mapped_v6_private_blocked() -> None:
    guard = NetGuard()
    with pytest.raises(BlockedAddress):
        guard.check_addresses("mapped.example", _ips("::ffff:10.0.0.1"))


def test_home_assistant_ip_blocked_even_if_public_looking() -> None:
    guard = NetGuard(home_assistant_ips=frozenset({"8.8.8.8"}))
    with pytest.raises(BlockedAddress, match="Home Assistant"):
        guard.check_addresses("ha.example", _ips("8.8.8.8"))


def test_blocked_host_by_name() -> None:
    guard = NetGuard(blocked_hosts=frozenset({"ha.local"}))
    with pytest.raises(BlockedAddress, match="blocked host"):
        guard.check_addresses("ha.local", _ips("203.0.113.7"))


def test_no_resolution_is_refused() -> None:
    with pytest.raises(BlockedAddress, match="does not resolve"):
        NetGuard().check_addresses("nowhere.example", [])
