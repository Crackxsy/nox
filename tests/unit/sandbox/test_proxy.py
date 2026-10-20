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


async def _echo_upstream() -> tuple[object, int]:
    import asyncio

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


async def test_tunnel_stays_open_after_the_connect_reply() -> None:
    # The 200 reply used to go through the error path, which also closed the client connection:
    # every tunnel ended before a single byte crossed it, so declared egress never worked.
    import asyncio

    server, echo_port = await _echo_upstream()
    proxy = EgressProxy(
        allow=("example.com:443",),
        guard=NetGuard(),
        resolver=lambda h, p: [ipaddress.ip_address("93.184.216.34")],
        open_connection=lambda h, p: asyncio.open_connection("127.0.0.1", echo_port),
    )
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert status.startswith(b"HTTP/1.1 200")
        writer.write(b"ping through the tunnel")
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(23), 5)
        assert echoed == b"ping through the tunnel"
        writer.close()
    finally:
        await proxy.stop()
        server.close()  # type: ignore[attr-defined]


def test_a_star_declaration_means_any_public_host_and_never_the_lan() -> None:
    """`EgressDecl(hosts=("*",))` (a network-enabled MCP server): any *public* host, any port,
    through the run's proxy - NetGuard still refuses loopback, LAN, metadata and Home Assistant."""
    table = {
        "example.com": "93.184.216.34",
        "router.lan": "192.168.0.1",
        "self.test": "127.0.0.1",
        "meta.test": "169.254.169.254",
        "ha.local": "93.184.216.99",
    }
    proxy = EgressProxy(
        allow=("*",),
        guard=NetGuard(home_assistant_ips=frozenset({"93.184.216.99"})),
        resolver=lambda h, p: [ipaddress.ip_address(table.get(h, h))],
    )
    assert proxy.authorize("example.com", 443).allowed
    assert proxy.authorize("example.com", 8443).allowed
    for host in ("router.lan", "self.test", "meta.test", "ha.local", "10.0.0.1", "::1"):
        assert not proxy.authorize(host, 443).allowed, host
