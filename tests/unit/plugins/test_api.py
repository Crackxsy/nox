"""Worker-side `PluginApi`: every boundary is the manifest (ST-11-01 acceptance criteria 6-8).

The scoped egress guard (ADR-013) is exercised through a fake transport, so no connection is ever
opened; a denied endpoint raises before the transport is reached.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from nox.core.state import PrivacyMode
from nox.plugins.api import PluginApi, PluginApiError, PrivacyView
from nox.plugins.manifest import parse_manifest
from nox.security.egress import EgressDenied
from nox.security.model import Risk
from tests.unit.plugins.conftest import VALID_MANIFEST


class FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.handlers: list[tuple[str, Any]] = []
        self.responses = responses or {}

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.requests.append((name, dict(payload or {})))
        return dict(self.responses.get(name, {}))

    async def send_event(
        self, name: str, payload: Mapping[str, Any] | None = None, *, corr: str | None = None
    ) -> None:
        self.events.append((name, dict(payload or {})))

    def on(self, name_glob: str, handler: Any) -> Any:
        entry = (name_glob, handler)
        self.handlers.append(entry)
        return lambda: self.handlers.remove(entry)


class PingInput(BaseModel):
    text: str = "ping"


def make_api(client: FakeClient | None = None, **manifest_overrides: Any) -> PluginApi:
    manifest = parse_manifest({**VALID_MANIFEST, **manifest_overrides})
    return PluginApi(manifest=manifest, client=client or FakeClient())


# ---- events --------------------------------------------------------------------------------------


async def test_emit_only_declared_events() -> None:
    client = FakeClient()
    api = make_api(client)
    await api.events.emit("demo.pong", {"text": "hi"})
    assert client.events == [("demo.pong", {"text": "hi"})]
    with pytest.raises(PluginApiError, match="events.emits"):
        await api.events.emit("demo.secret_leak", {})
    with pytest.raises(PluginApiError, match="events.emits"):
        await api.events.emit("security.kill_switch", {})
    assert len(client.events) == 1


def test_listen_only_declared_patterns() -> None:
    client = FakeClient()
    api = make_api(client)
    api.events.on("system.mode_changed", lambda name, payload: None)
    assert client.handlers[0][0] == "system.mode_changed"
    with pytest.raises(PluginApiError, match="events.listens"):
        api.events.on("voice.transcript_ready", lambda name, payload: None)


# ---- tools ---------------------------------------------------------------------------------------


async def test_tool_registration_and_call() -> None:
    api = make_api()

    async def handler(data: PingInput) -> dict[str, Any]:
        return {"echo": data.text}

    tool = api.tools.register("demo.ping", PingInput, handler, Risk.READ, description="d")
    assert tool.risk is Risk.READ and tool.side_effects is False
    assert api.tools.names() == ["demo.ping"]
    assert (await api.tools.call("demo.ping", {"text": "hello"})) == {"echo": "hello"}
    declaration = api.tools.declarations()[0]
    assert declaration["name"] == "demo.ping" and declaration["risk"] == "read"
    assert "text" in declaration["input_schema"]["properties"]


async def test_tool_namespace_and_declaration_are_enforced() -> None:
    api = make_api()

    async def handler(_data: PingInput) -> dict[str, Any]:
        return {}

    with pytest.raises(PluginApiError, match="namespace"):
        api.tools.register("twitch.chat.send", PingInput, handler, Risk.LOW)
    with pytest.raises(PluginApiError, match="not declared in the manifest"):
        api.tools.register("demo.undeclared", PingInput, handler, Risk.READ)
    assert api.tools.names() == []


async def test_tool_cannot_lower_the_manifest_risk() -> None:
    api = make_api(permissions=[{"tool": "demo.ping", "risk": "high"}])

    async def handler(_data: PingInput) -> dict[str, Any]:
        return {}

    with pytest.raises(PluginApiError, match="not allowed"):
        api.tools.register("demo.ping", PingInput, handler, Risk.READ)


async def test_tool_input_is_validated_in_the_worker() -> None:
    api = make_api()

    class StrictInput(BaseModel):
        count: int

    async def handler(data: StrictInput) -> dict[str, Any]:
        return {"count": data.count}

    api.tools.register("demo.ping", StrictInput, handler, Risk.READ)
    with pytest.raises(PluginApiError, match="invalid input"):
        await api.tools.call("demo.ping", {"count": "not a number"})


# ---- secrets / state -----------------------------------------------------------------------------


async def test_secret_get_only_for_declared_names() -> None:
    client = FakeClient({"plugin.secret.get": {"name": "nox/demo/token", "value": "s3cret"}})
    api = make_api(client, secrets=["nox/demo/token"])
    assert await api.secrets.get("nox/demo/token") == "s3cret"
    with pytest.raises(PluginApiError, match="not declared"):
        await api.secrets.get("nox/twitch/bot_oauth_token")
    # the undeclared name never reached the core
    assert [name for name, _ in client.requests] == ["plugin.secret.get"]


async def test_state_get_goes_through_ipc() -> None:
    client = FakeClient({"state.get": {"path": "system.level", "value": "running"}})
    api = make_api(client)
    assert await api.state.get("system.level") == "running"
    assert client.requests == [("state.get", {"path": "system.level"})]


# ---- egress (ADR-013) ----------------------------------------------------------------------------


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})


def make_guarded_api(
    egress: list[str], privacy: PrivacyView
) -> tuple[PluginApi, RecordingTransport]:
    transport = RecordingTransport()
    manifest = parse_manifest({**VALID_MANIFEST, "network": {"egress": egress}})
    api = PluginApi(
        manifest=manifest,
        client=FakeClient(),
        privacy=privacy,
        transport_factory=lambda: transport,
    )
    return api, transport


async def test_http_allows_only_declared_endpoints() -> None:
    api, transport = make_guarded_api(["api.example.com:443"], PrivacyView(PrivacyMode.BALANCED))
    async with api.http() as client:
        response = await client.get("https://api.example.com/v1/ping")
        assert response.status_code == 200
        with pytest.raises(EgressDenied, match="not_declared"):
            await client.get("https://evil.example.com/")
        with pytest.raises(EgressDenied, match="not_declared"):
            await client.get("https://api.example.com:8443/")
    assert transport.seen == ["https://api.example.com/v1/ping"]


async def test_http_still_obeys_privacy_mode() -> None:
    privacy = PrivacyView(PrivacyMode.BALANCED)
    api, _ = make_guarded_api(["api.example.com:443"], privacy)
    privacy.set(PrivacyMode.OFFLINE)
    async with api.http() as client:
        with pytest.raises(EgressDenied, match="offline"):
            await client.get("https://api.example.com/v1/ping")


async def test_declared_loopback_stays_reachable_in_private_mode() -> None:
    privacy = PrivacyView(PrivacyMode.PRIVATE)
    api, transport = make_guarded_api(["127.0.0.1:4455"], privacy)
    async with api.http() as client:
        assert (await client.get("http://127.0.0.1:4455/status")).status_code == 200
        with pytest.raises(EgressDenied):
            await client.get("http://127.0.0.1:9999/")
    assert transport.seen == ["http://127.0.0.1:4455/status"]


def test_http_without_declared_egress_is_refused_not_faked() -> None:
    api = make_api()
    with pytest.raises(PluginApiError, match="declares no network.egress"):
        api.http()


def test_egress_client_cannot_be_given_its_own_transport() -> None:
    api, _ = make_guarded_api(["api.example.com:443"], PrivacyView())
    with pytest.raises(ValueError, match="transport"):
        api.http(transport=httpx.AsyncHTTPTransport())
