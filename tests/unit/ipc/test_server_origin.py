"""The hub's DNS-rebinding defence: Host and Origin are checked before the WebSocket handshake.

A web page that rebinds its own name to 127.0.0.1 sends `Host: <its name>`; a page that simply
opens `ws://127.0.0.1:<port>/ws` sends its own `Origin`. Both are turned away with 403 before a
token is even looked at, while clients that send no Origin (shell, workers, plugins) and the core's
own pages keep working.
"""

from __future__ import annotations

import asyncio

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.typing import Origin

from nox.ipc.origin import ui_origins
from nox.ipc.protocol import Kind, Source
from nox.ipc.role_tokens import derive_role_token
from nox.ipc.server import IpcHub
from nox.ipc.tokens import TokenStore

from .conftest import HubFactory, RawClient

HTTP_PORT = 47801


async def _handshake_status(port: int, headers: list[tuple[str, str]]) -> int:
    """Send a WebSocket upgrade with exactly these headers and return the HTTP status code."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        lines = [
            "GET /ws HTTP/1.1",
            *(f"{name}: {value}" for name, value in headers),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
            "Sec-WebSocket-Version: 13",
        ]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout=5)
        return int(status_line.split()[1])
    finally:
        writer.close()


@pytest.fixture
async def ui_hub(hub_factory: HubFactory) -> IpcHub:
    hub = await hub_factory()
    hub.allow_browser_origins(ui_origins(HTTP_PORT))
    return hub


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example:{port}",  # DNS rebinding: the page's own name
        "127.evil.example:{port}",
        "localhost.evil:{port}",
        "127.0.0.1.nip.io:{port}",
        "127.0.0.1:1",  # this machine, but not the hub's port
    ],
)
async def test_a_rebinding_host_header_is_refused_before_the_handshake(
    ui_hub: IpcHub, host: str
) -> None:
    status = await _handshake_status(ui_hub.port, [("Host", host.format(port=ui_hub.port))])
    assert status == 403


async def test_a_doubled_host_header_is_refused(ui_hub: IpcHub) -> None:
    status = await _handshake_status(
        ui_hub.port,
        [("Host", f"127.0.0.1:{ui_hub.port}"), ("Host", f"attacker.example:{ui_hub.port}")],
    )
    assert status == 403


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}"])
async def test_every_spelling_of_this_machine_gets_the_handshake(ui_hub: IpcHub, host: str) -> None:
    status = await _handshake_status(ui_hub.port, [("Host", host.format(port=ui_hub.port))])
    assert status == 101


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "http://127.0.0.1:5173", "http://localhost:1", "null"],
)
async def test_a_foreign_browser_origin_is_refused(ui_hub: IpcHub, origin: str) -> None:
    with pytest.raises(InvalidStatus) as exc:
        await connect(ui_hub.url, origin=Origin(origin), open_timeout=5)
    assert exc.value.response.status_code == 403


async def test_no_browser_origin_is_accepted_before_the_ui_server_is_known(
    hub_factory: HubFactory,
) -> None:
    hub = await hub_factory()
    with pytest.raises(InvalidStatus) as exc:
        await connect(hub.url, origin=Origin(f"http://127.0.0.1:{HTTP_PORT}"), open_timeout=5)
    assert exc.value.response.status_code == 403


@pytest.mark.parametrize(
    "origin", [f"http://127.0.0.1:{HTTP_PORT}", f"http://localhost:{HTTP_PORT}"]
)
async def test_the_cores_own_pages_connect_and_authenticate(
    ui_hub: IpcHub, tokens: TokenStore, origin: str
) -> None:
    async with connect(ui_hub.url, origin=Origin(origin), open_timeout=5) as conn:
        client = RawClient(conn, Source(role="dashboard", id="dashboard:1"))
        # The dashboard's own role token: the shell's session token is refused for any other role.
        reply = await client.auth(derive_role_token(tokens.session_token, "dashboard"))
        assert reply.kind is Kind.RESPONSE


async def test_a_client_without_origin_is_a_native_client_and_connects(
    ui_hub: IpcHub, tokens: TokenStore
) -> None:
    async with connect(ui_hub.url, open_timeout=5) as conn:
        client = RawClient(conn, Source(role="shell", id="shell:1"))
        reply = await client.auth(tokens.session_token)
        assert reply.kind is Kind.RESPONSE
