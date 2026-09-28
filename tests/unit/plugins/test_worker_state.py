"""A plugin worker starts from the core's *current* privacy, capture and kill state.

Events only carry changes. A privacy mode set, a capture flag closed or a kill switch engaged
before the plugin connected used to be invisible to it: the worker believed BALANCED, its scoped
egress guard let a declared cloud endpoint through while the user had chosen OFFLINE, and a
plugin whose kill had already happened started anyway. These tests set the state *before* the
worker registers and check that it is honoured from the first request on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from nox.core.state import PrivacyMode
from nox.ipc.protocol import Envelope, Kind, Source
from nox.plugins.api import PluginApi, PrivacyView
from nox.plugins.manifest import parse_manifest
from nox.security.constants import fail_closed_connect_state
from nox.security.egress import EgressDenied
from nox.worker.plugin import PluginWorker
from tests.unit.plugins.conftest import VALID_MANIFEST

CLOUD_ENDPOINT = "api.example.com:443"


class WorkerClient:
    """The worker's IPC client: answers `plugin.register` with a scripted core state."""

    def __init__(self, register_response: Mapping[str, Any]) -> None:
        self.register_response = dict(register_response)
        self.requests: list[str] = []
        self.handlers: dict[str, Any] = {}
        self.closed = False

    async def connect(self) -> None:
        return None

    async def subscribe(self, _patterns: list[str]) -> None:
        return None

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.requests.append(name)
        if name == "plugin.register":
            return dict(self.register_response)
        return {"ok": True}

    async def send_event(self, name: str, payload: Mapping[str, Any] | None = None) -> None:
        return None

    def handle(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler

    def on(self, name_glob: str, handler: Any) -> Any:
        self.handlers[name_glob] = handler
        return lambda: None

    async def close(self) -> None:
        self.closed = True


class RecordingPlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        self.started = False
        self.privacy_at_start: PrivacyMode | None = None

    async def start(self) -> None:
        self.started = True
        self.privacy_at_start = self.api.privacy.mode

    async def stop(self) -> None:
        return None


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        return httpx.Response(200)


def make_worker(
    register_response: Mapping[str, Any],
) -> tuple[PluginWorker, WorkerClient, RecordingTransport, dict[str, RecordingPlugin]]:
    manifest = parse_manifest({**VALID_MANIFEST, "network": {"egress": [CLOUD_ENDPOINT]}})
    client = WorkerClient(register_response)
    transport = RecordingTransport()
    created: dict[str, RecordingPlugin] = {}

    def factory(api: PluginApi) -> RecordingPlugin:
        created["plugin"] = RecordingPlugin(api)
        return created["plugin"]

    worker = PluginWorker(client=client, manifest=manifest, factory=factory, heartbeat_s=60)
    worker.api = PluginApi(
        manifest=manifest,
        client=client,
        privacy=worker.privacy,
        transport_factory=lambda: transport,
    )
    return worker, client, transport, created


def core_state(
    mode: PrivacyMode, *, screen: bool = True, safe_mode: bool = False
) -> dict[str, Any]:
    return {
        "ok": True,
        "config": {},
        "privacy_mode": mode.value,
        "capture": {"microphone": True, "camera": False, "screen": screen, "cloud": True},
        "safe_mode": safe_mode,
    }


async def _run_until_running(worker: PluginWorker) -> None:
    """Run the worker until it is up (or has refused to start), then stop it."""
    task = asyncio.create_task(worker.run())
    for _ in range(200):
        if worker.status in ("running", "safe_mode") or task.done():
            break
        await asyncio.sleep(0.005)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=5)


def test_a_fresh_view_is_closed_until_the_core_answers() -> None:
    view = PrivacyView()
    assert view.mode is PrivacyMode.OFFLINE
    assert view.capture == {"microphone": False, "camera": False, "screen": False, "cloud": False}
    assert not view.allows_capture("screen")


async def test_offline_set_before_registration_blocks_the_plugins_egress() -> None:
    worker, _client, transport, created = make_worker(core_state(PrivacyMode.OFFLINE))

    await _run_until_running(worker)

    plugin = created["plugin"]
    assert plugin.started and plugin.privacy_at_start is PrivacyMode.OFFLINE
    async with worker.api.http() as http:
        with pytest.raises(EgressDenied, match="offline"):
            await http.get("https://api.example.com/v1/ping")
    assert transport.seen == []


async def test_balanced_from_the_core_lets_a_declared_endpoint_through() -> None:
    worker, _client, transport, _created = make_worker(core_state(PrivacyMode.BALANCED))

    await _run_until_running(worker)

    async with worker.api.http() as http:
        assert (await http.get("https://api.example.com/v1/ping")).status_code == 200
    assert transport.seen == ["https://api.example.com/v1/ping"]


async def test_a_kill_engaged_before_registration_never_starts_the_plugin() -> None:
    worker, client, _transport, created = make_worker(
        core_state(PrivacyMode.BALANCED, safe_mode=True)
    )

    await _run_until_running(worker)

    assert created["plugin"].started is False
    assert worker.status in ("safe_mode", "stopping")
    assert client.closed is True
    assert "worker.heartbeat" not in client.requests


async def test_a_core_that_sends_no_state_gets_the_strictest_reading() -> None:
    worker, _client, _transport, created = make_worker({"ok": True, "config": {}})

    await _run_until_running(worker)

    assert worker.privacy.mode is PrivacyMode.OFFLINE
    assert worker.privacy.safe_mode is True
    assert created["plugin"].started is False


async def test_capture_state_from_registration_and_later_changes_reach_the_view() -> None:
    worker, client, _transport, _created = make_worker(
        core_state(PrivacyMode.BALANCED, screen=False)
    )

    await _run_until_running(worker)
    assert worker.api.privacy.allows_capture("screen") is False

    envelope = Envelope(
        kind=Kind.EVENT,
        name="privacy.capture_changed",
        src=Source(role="core", id="core"),
        payload={"microphone": False, "camera": False, "screen": True, "cloud": False},
    )
    await client.handlers["privacy.capture_changed"](envelope)
    assert worker.api.privacy.allows_capture("screen") is True


async def test_the_scoped_guard_follows_the_kill_switch() -> None:
    view = PrivacyView(PrivacyMode.BALANCED)
    manifest = parse_manifest({**VALID_MANIFEST, "network": {"egress": [CLOUD_ENDPOINT]}})
    api = PluginApi(manifest=manifest, client=WorkerClient({}), privacy=view)
    assert api.egress.check("api.example.com", 443).allowed

    view.set_safe_mode(True)

    decision = api.egress.check("api.example.com", 443)
    assert not decision.allowed and decision.rule_id == "safe_mode"
    assert not view.allows_capture("screen")


def test_fail_closed_connect_state_parses_to_a_closed_view() -> None:
    view = PrivacyView(PrivacyMode.FULL, capture={"screen": True})
    view.apply(fail_closed_connect_state())
    assert view.mode is PrivacyMode.OFFLINE and view.safe_mode is True
    assert not view.allows_capture("screen")
