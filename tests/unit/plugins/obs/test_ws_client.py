"""`ObsWebSocketClient` against the fake obs-websocket v5 server: Hello/Identify auth, request/
response correlation, event dispatch, reconnect with backoff."""

from __future__ import annotations

import asyncio

import pytest
from nox_plugin_obs.ws_client import (
    DEFAULT_EVENT_SUBSCRIPTIONS,
    ObsAuthRequiredError,
    ObsRequestError,
    ObsWebSocketClient,
)

from .fake_obs_server import FakeObsServer

pytestmark = pytest.mark.timeout(30)


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _make_client(server: FakeObsServer, password: str | None, **kwargs) -> ObsWebSocketClient:
    events: list[tuple[str, dict]] = []

    async def on_event(name: str, data: dict) -> None:
        events.append((name, data))

    client = ObsWebSocketClient(
        server.url,
        password_provider=lambda: _const(password),
        on_event=on_event,
        min_backoff_s=0.05,
        max_backoff_s=0.2,
        **kwargs,
    )
    client.events = events  # type: ignore[attr-defined]
    return client


async def _const(value):
    return value


async def test_connects_and_authenticates_with_the_correct_password(obs_server) -> None:
    client = _make_client(obs_server, "s3cret")
    client.start()
    try:
        await _wait_until(lambda: client.identified)
        assert client.connected is True
    finally:
        await client.stop()


async def test_wrong_password_never_identifies(obs_server) -> None:
    client = _make_client(obs_server, "wrong")
    client.start()
    try:
        await asyncio.sleep(0.3)
        assert client.identified is False
        assert client.last_error
    finally:
        await client.stop()


async def test_missing_password_raises_auth_required_and_is_reported(obs_server) -> None:
    client = _make_client(obs_server, None)
    client.start()
    try:
        await _wait_until(lambda: bool(client.last_error))
        assert "password" in client.last_error.lower()
        assert client.identified is False
    finally:
        await client.stop()


async def test_request_response_round_trip(obs_server) -> None:
    obs_server.set_response(
        "GetSceneList", lambda _d: {"currentProgramSceneName": "Live", "scenes": []}
    )
    client = _make_client(obs_server, "s3cret")
    client.start()
    try:
        await _wait_until(lambda: client.identified)
        result = await client.request("GetSceneList")
        assert result["currentProgramSceneName"] == "Live"
        assert obs_server.requests[-1][0] == "GetSceneList"
    finally:
        await client.stop()


async def test_request_failure_raises_obs_request_error(obs_server) -> None:
    obs_server.set_response("SetCurrentProgramScene", RuntimeError("no such scene"))
    client = _make_client(obs_server, "s3cret")
    client.start()
    try:
        await _wait_until(lambda: client.identified)
        with pytest.raises(ObsRequestError):
            await client.request("SetCurrentProgramScene", {"sceneName": "Nope"})
    finally:
        await client.stop()


async def test_events_are_dispatched(obs_server) -> None:
    client = _make_client(obs_server, "s3cret")
    client.start()
    try:
        await _wait_until(lambda: client.identified)
        await obs_server.broadcast_event("CurrentProgramSceneChanged", {"sceneName": "Live"})
        await _wait_until(lambda: bool(client.events))  # type: ignore[attr-defined]
        assert client.events[0] == ("CurrentProgramSceneChanged", {"sceneName": "Live"})  # type: ignore[attr-defined]
    finally:
        await client.stop()


async def test_reconnects_with_backoff_after_the_server_drops(obs_server) -> None:
    client = _make_client(obs_server, "s3cret")
    client.start()
    try:
        await _wait_until(lambda: client.identified)
        first_identify_count = obs_server.identify_count
        # Force-close every live connection from the server side.
        for ws in list(obs_server._clients):
            await ws.close()
        await _wait_until(lambda: client.identified is False)
        await _wait_until(lambda: obs_server.identify_count > first_identify_count, timeout=5.0)
        assert client.identified is True
    finally:
        await client.stop()


async def test_event_subscriptions_default_to_general_scenes_outputs() -> None:
    assert DEFAULT_EVENT_SUBSCRIPTIONS == (1 | 4 | 64)


def test_auth_required_error_type() -> None:
    assert issubclass(ObsAuthRequiredError, RuntimeError)
