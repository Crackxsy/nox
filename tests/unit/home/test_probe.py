"""The "Verbindung testen" probe: four different failures, four different answers.

A connection test that says "failed" for a wrong token, a blocked privacy mode and an unplugged
network cable is not a connection test - the whole point is that the Settings page can tell the
user which of those it was.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from nox.core.config import HomeConfig
from nox.home.probe import probe_home_assistant
from nox.security.egress import EgressDenied

TOKEN = "test-token"  # noqa: S105 - a test fixture value, never a real credential
SETTINGS = HomeConfig(host="127.0.0.1", port=8123)


Factory = Callable[..., httpx.AsyncClient]


def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> Factory:
    def build(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    return build


def _home_assistant(request: httpx.Request) -> httpx.Response:
    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return httpx.Response(401, json={"message": "Unauthorized"})
    if request.url.path == "/api/":
        return httpx.Response(200, json={"message": "API running."})
    if request.url.path == "/api/config":
        return httpx.Response(200, json={"version": "2026.9.0", "location_name": "Zuhause"})
    return httpx.Response(404)  # pragma: no cover - the probe asks for nothing else


async def test_a_reachable_instance_reports_its_version() -> None:
    result = await probe_home_assistant(SETTINGS, TOKEN, _factory(_home_assistant))
    assert result.ok is True
    assert result.code == "ok"
    assert result.ha_version == "2026.9.0"


async def test_a_wrong_token_is_unauthorized_not_unreachable() -> None:
    result = await probe_home_assistant(SETTINGS, "wrong", _factory(_home_assistant))
    assert result.ok is False
    assert result.code == "unauthorized"


async def test_no_stored_token_is_its_own_answer() -> None:
    result = await probe_home_assistant(SETTINGS, None, _factory(_home_assistant))
    assert result.ok is False
    assert result.code == "no_token"


async def test_nothing_listening_is_unreachable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await probe_home_assistant(SETTINGS, TOKEN, _factory(refuse))
    assert result.ok is False
    assert result.code == "unreachable"
    assert result.detail == "ConnectError"


async def test_a_privacy_mode_or_profile_that_blocks_the_host_says_blocked() -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        raise EgressDenied("127.0.0.1", 8123, "privacy.private", "PRIVATE: only loopback")

    result = await probe_home_assistant(SETTINGS, TOKEN, _factory(denied))
    assert result.ok is False
    assert result.code == "blocked"
    assert "PRIVATE" in result.detail


async def test_something_else_on_that_port_is_reported_by_status_code() -> None:
    def other_service(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    result = await probe_home_assistant(SETTINGS, TOKEN, _factory(other_service))
    assert result.ok is False
    assert result.code == "http_error"
    assert result.detail == "500"


async def test_an_unreadable_config_endpoint_still_counts_as_a_working_connection() -> None:
    """A token allowed to call `/api/` but not `/api/config` is unusual but not a failure."""

    def partial(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        return httpx.Response(403)

    result = await probe_home_assistant(SETTINGS, TOKEN, _factory(partial))
    assert result.ok is True
    assert result.ha_version == ""


@pytest.mark.parametrize("tls", [False, True])
async def test_the_scheme_follows_the_tls_setting(tls: bool) -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"message": "API running."})

    await probe_home_assistant(HomeConfig(tls=tls), TOKEN, _factory(record))
    assert seen[0].startswith("https://" if tls else "http://")
