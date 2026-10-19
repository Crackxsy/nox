"""`EgressProxy.authorize`: a CONNECT passes only when declared AND its address is allowed.

The two negative cases: a host not in the run's declaration, and a declared host that resolves to
the LAN - both must be refused, and an audited decision recorded.
"""

from __future__ import annotations

import ipaddress

from nox.sandbox.netguard import NetGuard
from nox.sandbox.proxy import EgressProxy


def _proxy(allow, resolver):  # noqa: ANN001
    decisions: list = []
    proxy = EgressProxy(
        allow=allow,
        guard=NetGuard(),
        audit=decisions.append,
        resolver=resolver,
    )
    return proxy, decisions


def test_allowed_public_host() -> None:
    proxy, _ = _proxy(("example.com:443",), lambda h, p: [ipaddress.ip_address("93.184.216.34")])
    decision = proxy.authorize("example.com", 443)
    assert decision.allowed


def test_undeclared_host_refused() -> None:
    proxy, _ = _proxy(("example.com:443",), lambda h, p: [ipaddress.ip_address("93.184.216.34")])
    decision = proxy.authorize("other.com", 443)
    assert not decision.allowed
    assert "declaration" in decision.reason


def test_declared_host_resolving_to_lan_refused() -> None:
    # Declared, but DNS points it at the LAN: NetGuard must still refuse (rebind defence).
    proxy, _ = _proxy(("intranet.example:443",), lambda h, p: [ipaddress.ip_address("192.168.0.9")])
    decision = proxy.authorize("intranet.example", 443)
    assert not decision.allowed
    assert "LAN" in decision.reason or "private" in decision.reason


def test_wrong_port_refused() -> None:
    proxy, _ = _proxy(("example.com:443",), lambda h, p: [ipaddress.ip_address("93.184.216.34")])
    assert not proxy.authorize("example.com", 8443).allowed
