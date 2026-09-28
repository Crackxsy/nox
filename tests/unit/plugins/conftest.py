"""Shared fixtures/fakes for the plugin runtime tests: manifests on disk, a fake hub and a fake
permission engine. No real process, no real socket, no real keyring."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.ipc.plugin_scope import PluginScope
from nox.security.model import Decision, PermissionRequest, PermissionResult, Profile

VALID_MANIFEST: dict[str, Any] = {
    "id": "demo",
    "name": "Demo",
    "version": "0.1.0",
    "api_version": 1,
    "entry": "nox_plugin_demo:create",
    "profiles": [],
    "permissions": [{"tool": "demo.ping", "risk": "read"}],
    "events": {"emits": ["demo.pong"], "listens": ["system.mode_changed"]},
    "secrets": [],
    "network": {"egress": []},
    "resources": {"memory_mb": 64, "priority": "normal"},
}


def write_manifest(root: Path, plugin_id: str, **overrides: Any) -> Path:
    """Write `<root>/<plugin_id>/manifest.yaml` from the valid baseline plus overrides."""
    data = {**VALID_MANIFEST, "id": plugin_id, **overrides}
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def make_profile(profile_id: str = "companion", **overrides: Any) -> Profile:
    data: dict[str, Any] = {"id": profile_id, "rules": [], **overrides}
    return Profile.model_validate(data)


class FakeHub:
    """Records the plugin scopes it is given and answers `request` from a scripted map."""

    def __init__(self, url: str = "ws://127.0.0.1:47800/ws") -> None:
        self._url = url
        self.scopes: dict[str, PluginScope] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        #: Client ids `find_client` reports as connected right now.
        self.connected: set[str] = set()

    @property
    def url(self) -> str:
        return self._url

    def set_plugin_scope(self, client_id: str, scope: PluginScope) -> None:
        self.scopes[client_id] = scope

    def find_client(self, client_id: str) -> Any:
        return client_id if client_id in self.connected else None

    async def request(
        self,
        client_id: str,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        on_stream: Any = None,
    ) -> dict[str, Any]:
        self.requests.append((client_id, name, dict(payload or {})))
        result = self.responses.get(name, {"ok": True})
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return dict(result(payload or {}))
        return dict(result)


class FakeEngine:
    def __init__(self, profile: Profile | None = None, decision: Decision = Decision.ALLOW) -> None:
        self.profile = profile or make_profile()
        self.decision = decision
        self.checked: list[PermissionRequest] = []

    def check(self, request: PermissionRequest) -> PermissionResult:
        self.checked.append(request)
        return PermissionResult(decision=self.decision, rule_id="test.rule")

    def active_profile(self) -> Profile:
        return self.profile


class FakeTokens:
    def __init__(self) -> None:
        self.issued: list[str] = []
        self.bound: list[tuple[str, int]] = []
        self.revoked: list[str] = []

    def worker_env(self, worker_id: str, *, ttl_s: float | None = None) -> Mapping[str, str]:
        self.issued.append(worker_id)
        return {"NOX_WORKER_TOKEN": "t" * 40}

    def bind_worker_process(self, worker_id: str, pid: int) -> None:
        self.bound.append((worker_id, pid))

    def revoke_worker(self, worker_id: str) -> None:
        self.revoked.append(worker_id)


class FakeProcess:
    """A process that is alive until `exit(code)` is called."""

    def __init__(self, pid: int = 4242) -> None:
        self._pid = pid
        self._code: int | None = None
        self.terminated = False
        self.killed = False

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return self._code

    def exit(self, code: int = 1) -> None:
        self._code = code

    def terminate(self) -> None:
        self.terminated = True
        if self._code is None:
            self._code = -15

    def kill(self) -> None:
        self.killed = True
        self._code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._code if self._code is not None else 0


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "plugins"
    directory.mkdir()
    return directory
