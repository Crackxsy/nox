"""Fixtures shared by the `coding` plugin's unit tests: a real `PluginApi` wired to a `FakeClient`
(same structural pattern as `tests/unit/plugins/obs/conftest.py`'s `FakeClient`/`make_api`), plus a
fake `claude` executable (`fake_claude.py`) invoked via `command_override=[sys.executable, script]`
- the same convention `tests/unit/ai/test_claude_code.py` uses for `ClaudeCodeProvider`. No real
process talks to the real Claude Code CLI in this package."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nox.ipc.protocol import Envelope, Kind, Source
from nox.plugins.api import PluginApi
from nox.plugins.manifest import parse_manifest

# The plugin worker inserts `plugins/coding/src` into `sys.path` itself at spawn time
# (`nox.worker.plugin.load_entry`); these unit tests import `nox_plugin_coding` directly
# in-process, so they need the same path entry.
_CODING_SRC = Path(__file__).resolve().parents[4] / "plugins" / "coding" / "src"
if str(_CODING_SRC) not in sys.path:
    sys.path.insert(0, str(_CODING_SRC))

CODING_MANIFEST: dict[str, Any] = {
    "id": "coding",
    "name": "Coding Assistant",
    "version": "0.1.0",
    "api_version": 1,
    "entry": "nox_plugin_coding:create",
    "profiles": ["coding"],
    "permissions": [
        {"tool": "coding.session.start", "risk": "medium"},
        {"tool": "coding.session.status.read", "risk": "read"},
        {"tool": "coding.session.stop", "risk": "low"},
        {"tool": "coding.review.request", "risk": "medium"},
    ],
    "events": {
        "emits": [
            "coding.session_started",
            "coding.session_progress",
            "coding.session_ended",
            "coding.session_failed",
        ],
        "listens": ["security.kill_switch"],
    },
    "secrets": [],
    "network": {"egress": []},
    "resources": {"memory_mb": 256, "priority": "normal"},
    "config": {
        "command": "claude",
        "model": "sonnet",
        "permission_mode": "acceptEdits",
        "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep"],
        "max_turns": 30,
        "max_repair_attempts": 3,
        "session_timeout_s": 1800,
        "filesystem_roots": [],
    },
}


class FakeClient:
    """Minimal `PluginClient`: records emitted events, lets tests fire `client.on(...)` handlers
    directly (see `PluginApi.events`/`nox.plugins.api.PluginClient`)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, list[Any]] = {}

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        return {}

    async def send_event(
        self, name: str, payload: Mapping[str, Any] | None = None, *, corr: str | None = None
    ) -> None:
        self.events.append((name, dict(payload or {})))

    def on(self, name_glob: str, handler: Any) -> Any:
        self.handlers.setdefault(name_glob, []).append(handler)

        def _unsub() -> None:
            self.handlers[name_glob].remove(handler)

        return _unsub

    async def fire(self, name: str, payload: dict[str, Any] | None = None) -> None:
        env = Envelope(
            kind=Kind.EVENT,
            name=name,
            src=Source(role="core", id="core"),
            payload=dict(payload or {}),
        )
        for handler in list(self.handlers.get(name, [])):
            result = handler(env)
            if result is not None:
                await result


def make_manifest(**overrides: Any):
    config = {**CODING_MANIFEST["config"], **overrides.pop("config", {})}
    return parse_manifest({**CODING_MANIFEST, **overrides, "config": config})


def make_api(client: FakeClient, **config_overrides: Any) -> PluginApi:
    manifest = make_manifest(config=config_overrides)
    return PluginApi(manifest=manifest, client=client)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def fake_cli_command() -> list[str]:
    """`command_override` for `SessionRunner`: run the fake CLI under this interpreter."""
    script = Path(__file__).parent / "fake_claude.py"
    return [sys.executable, str(script)]
