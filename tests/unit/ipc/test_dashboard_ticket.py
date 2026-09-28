"""Opening the dashboard without a token on any command line, and without the shell's token.

The shell trades its own token for a one-time ticket, the browser redeems the ticket once and is
redirected to the dashboard with the dashboard's *own* token in the fragment. A pet or dashboard
token can never claim the shell role on the hub, so a page cannot answer a permission
confirmation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from nox.ipc.http import HttpServer, HttpSettings, OneTimeTickets, create_app
from nox.ipc.protocol import Kind
from nox.ipc.role_tokens import derive_role_token
from nox.ipc.server import IpcHub
from nox.ipc.tokens import TokenStore
from nox.shell.runtime import DashboardTicketError, request_dashboard_ticket
from tests.unit.fakes import MonotonicClock
from tests.unit.ipc.conftest import RawClient

SESSION = "session-token-for-tests-0123456789"
DASHBOARD = derive_role_token(SESSION, "dashboard")


def _app(tickets: OneTimeTickets | None = None) -> Any:
    return create_app(
        health=lambda: {},
        state=lambda _path: {},
        providers=lambda: [],
        session_token=lambda: SESSION,
        dashboard_token=lambda: DASHBOARD,
        tickets=tickets,
    )


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _ticket(client: httpx.AsyncClient, token: str = SESSION) -> httpx.Response:
    return await client.post(
        "/api/ui/dashboard-ticket", headers={"Authorization": f"Bearer {token}"}
    )


async def test_a_ticket_opens_the_dashboard_once_with_the_dashboard_token() -> None:
    async with _client(_app()) as client:
        ticket = (await _ticket(client)).json()["ticket"]

        first = await client.get("/open", params={"ticket": ticket})
        second = await client.get("/open", params={"ticket": ticket})

    assert first.status_code == 303
    assert first.headers["location"] == f"/dashboard/#token={DASHBOARD}"
    assert SESSION not in first.headers["location"]
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["referrer-policy"] == "no-referrer"
    assert second.status_code == 403
    assert DASHBOARD not in second.text


async def test_only_the_shell_token_gets_a_ticket() -> None:
    async with _client(_app()) as client:
        missing = await client.post("/api/ui/dashboard-ticket")
        with_dashboard_token = await _ticket(client, DASHBOARD)
        with_pet_token = await _ticket(client, derive_role_token(SESSION, "pet"))

    assert [r.status_code for r in (missing, with_dashboard_token, with_pet_token)] == [401] * 3


async def test_an_unknown_or_expired_ticket_is_refused() -> None:
    clock = MonotonicClock()
    async with _client(_app(OneTimeTickets(ttl_s=30.0, clock=clock))) as client:
        ticket = (await _ticket(client)).json()["ticket"]
        clock.advance(31.0)
        expired = await client.get("/open", params={"ticket": ticket})
        unknown = await client.get("/open", params={"ticket": "made-up"})
        empty = await client.get("/open")

    assert [r.status_code for r in (expired, unknown, empty)] == [403, 403, 403]


def test_outstanding_tickets_are_bounded() -> None:
    tickets = OneTimeTickets(max_outstanding=2)
    first, second, third = tickets.issue(), tickets.issue(), tickets.issue()
    assert tickets.redeem(first) is False  # the oldest made room
    assert tickets.redeem(second) and tickets.redeem(third)


async def test_the_shell_fetches_a_ticket_over_loopback_ignoring_any_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # would swallow the bearer token
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    server = HttpServer(_app(), HttpSettings(port=0))
    await server.start()
    try:
        ticket = await asyncio.to_thread(
            request_dashboard_ticket, server.port, "127.0.0.1", SESSION
        )
        with pytest.raises(DashboardTicketError):
            await asyncio.to_thread(request_dashboard_ticket, server.port, "127.0.0.1", "wrong")
    finally:
        await server.stop()
    assert ticket


@pytest.mark.parametrize(("presented", "claimed"), [("pet", "shell"), ("dashboard", "shell")])
async def test_a_ui_page_cannot_connect_as_the_shell(
    hub: IpcHub, raw: Any, tokens: TokenStore, presented: str, claimed: str
) -> None:
    client: RawClient = await raw(claimed, f"{claimed}:page")
    reply = await client.auth(tokens.role_token(presented))
    assert reply.kind is Kind.ERROR and reply.payload["code"] == "auth.denied"


async def test_the_dashboard_token_cannot_answer_a_permission_request(
    hub: IpcHub, raw: Any, tokens: TokenStore
) -> None:
    client: RawClient = await raw("dashboard", "dashboard:1")
    assert (await client.auth(tokens.role_token("dashboard"))).kind is Kind.RESPONSE
    reply = await client.request("security.permission.reply", {})
    assert reply.kind is Kind.ERROR and reply.payload["code"] == "permission.denied"
