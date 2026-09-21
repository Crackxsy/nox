"""`install(core)`: the IPC surface, its roles, and the live appliers against a stand-in core."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.core.config import NoxConfig, load_config
from nox.core.state import PrivacyMode
from nox.ipc.dispatch import RequestRegistry, role_allows
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.protocol import Envelope, Kind, Source
from nox.security.gate import SecurityChangeGate
from nox.security.secrets import InMemorySecretStore, PinManager
from nox.settings.install import UI_ROLES, install
from nox.settings.secrets_ipc import KNOWN_SECRETS
from tests.unit.fakes import FakeBus
from tests.unit.settings.conftest import FakeAudit

SETTINGS_REQUESTS = (
    "config.get",
    "config.set",
    "security.pin.status",
    "secrets.status",
    "secrets.set",
    "secrets.delete",
    "twitch.auth.start",
    "twitch.auth.status",
    "twitch.auth.disconnect",
    "personality.get",
    "personality.set",
)


class FakePrivacy:
    def __init__(self) -> None:
        self.mode = PrivacyMode.BALANCED
        self.captures: list[dict[str, Any]] = []

    async def set_capture(self, *, by: str = "user", **kinds: Any) -> dict[str, Any]:
        self.captures.append({**kinds, "by": by})
        return kinds


class FakeEgress:
    def client(self, **_kwargs: Any) -> Any:  # pragma: no cover - never called in these tests
        raise AssertionError("no test in here reaches the network")


class FakeSecurity:
    def __init__(self, secrets: InMemorySecretStore, audit: FakeAudit) -> None:
        self.secrets = secrets
        self.audit = audit
        self.pin = PinManager(secrets)
        self.privacy = FakePrivacy()
        self.egress = FakeEgress()
        # The settings editor asks the gate before it writes a security or privacy setting.
        self.gate = SecurityChangeGate(self.pin, required=True, audit=audit)


class FakeOrchestratorConfig:
    default_language = "de"


class FakeOrchestrator:
    def __init__(self) -> None:
        self.config = FakeOrchestratorConfig()


class FakeCore:
    def __init__(self, config: NoxConfig, bus: FakeBus, security: FakeSecurity) -> None:
        self.config = config
        self.bus = bus
        self.security = security
        self.registry = RequestRegistry()
        self.orchestrator = FakeOrchestrator()


@pytest.fixture
def core(tmp_path: Path, bus: FakeBus, secrets: InMemorySecretStore, audit: FakeAudit) -> FakeCore:
    config = load_config(Path(__file__).resolve().parents[3] / "config" / "defaults.yaml")
    config.paths.data_dir = tmp_path / "data"
    return FakeCore(config, bus, FakeSecurity(secrets, audit))


async def _call(core: FakeCore, name: str, payload: dict[str, Any], role: str = "dashboard") -> Any:
    reg = core.registry.get(name)
    assert reg is not None, f"{name} is not registered"
    envelope = Envelope(
        kind=Kind.REQUEST, name=name, src=Source(role=role, id=role), payload=payload
    )
    from nox.ipc.dispatch import RequestContext

    ctx = RequestContext(client_id=role, role=role, request=envelope)
    return await reg.handler(ctx, reg.payload_model.model_validate(payload))


async def test_install_registers_the_settings_requests_for_ui_roles_only(
    core: FakeCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOX_USER_CONFIG", str(tmp_path / "user.yaml"))
    runtime = install(core)
    try:
        for name in SETTINGS_REQUESTS:
            reg = core.registry.get(name)
            assert reg is not None
            assert reg.allowed_roles == frozenset(UI_ROLES)
            # The hub's own per-role name allow-list has to know the name too.
            assert role_allows("dashboard", name)
            assert role_allows("shell", name)
            assert not role_allows("plugin", name)
            assert not role_allows("pet", name)
            assert not role_allows("remote", name)
    finally:
        await runtime.stop()


async def test_personality_file_is_created_in_the_data_dir_and_is_readable_over_ipc(
    core: FakeCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOX_USER_CONFIG", str(tmp_path / "user.yaml"))
    runtime = install(core)
    try:
        assert (core.config.paths.data_dir / "personality.md").is_file()

        result = await _call(core, "personality.get", {})
        assert result["path"] == str(core.config.paths.data_dir / "personality.md")
        assert result["text"].startswith("You are Nox.")

        await _call(core, "personality.set", {"text": "You are Nox. Short answers."})
        assert (await _call(core, "personality.get", {}))["text"] == "You are Nox. Short answers."
        assert core.security.audit.entries[-1]["action"] == "personality.set"
    finally:
        await runtime.stop()


async def test_config_set_live_applies_capture_through_the_privacy_service(
    core: FakeCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOX_USER_CONFIG", str(tmp_path / "user.yaml"))
    runtime = install(core)
    try:
        result = await _call(
            core,
            "config.set",
            {"values": {"privacy.capture.camera": True, "identity.ui_language": "en"}},
        )

        assert sorted(result["applied"]) == ["identity.ui_language", "privacy.capture.camera"]
        assert result["restart_required"] == []
        assert core.security.privacy.captures == [{"camera": True, "by": "settings"}]
        assert core.config.privacy.capture.camera is True
        assert core.orchestrator.config.default_language == "en"
    finally:
        await runtime.stop()


async def test_secrets_requests_refuse_an_unknown_name(
    core: FakeCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOX_USER_CONFIG", str(tmp_path / "user.yaml"))
    runtime = install(core)
    try:
        with pytest.raises(IpcError):
            await _call(core, "secrets.set", {"name": "nox/security/pin", "value": "1234"})
        status = await _call(core, "secrets.status", {})
        # One row per known name, all absent - derived from `KNOWN_SECRETS` rather than a literal
        # count, so adding an integration's credential does not fail this unrelated assertion.
        assert [entry["present"] for entry in status["secrets"]] == [False] * len(KNOWN_SECRETS)
    finally:
        await runtime.stop()


async def test_pin_status_reports_whether_a_pin_is_configured_and_nothing_else(
    core: FakeCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#23: the Settings page asks this before it offers a PIN field for a secret change."""
    monkeypatch.setenv("NOX_USER_CONFIG", str(tmp_path / "user.yaml"))
    runtime = install(core)
    try:
        assert await _call(core, "security.pin.status", {}) == {"configured": False}

        core.security.pin.set_pin("4711", by="test")

        answer = await _call(core, "security.pin.status", {})
        assert answer == {"configured": True}
        # Presence only: no hash, no length, no algorithm, and certainly no PIN.
        assert "4711" not in repr(answer)
    finally:
        await runtime.stop()


async def test_a_secret_change_needs_the_pin_once_one_is_configured(
    core: FakeCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal the dashboard turns into `pin_required`/`pin_wrong` (#23)."""
    monkeypatch.setenv("NOX_USER_CONFIG", str(tmp_path / "user.yaml"))
    runtime = install(core)
    try:
        core.security.pin.set_pin("4711", by="test")
        name = "nox/obs/websocket_password"

        with pytest.raises(IpcError) as without_pin:
            await _call(core, "secrets.set", {"name": name, "value": "pw"})
        assert without_pin.value.code == ERR_PERMISSION
        assert "PIN" in without_pin.value.message

        with pytest.raises(IpcError):
            await _call(core, "secrets.set", {"name": name, "value": "pw", "pin": "0000"})

        assert await _call(core, "secrets.set", {"name": name, "value": "pw", "pin": "4711"}) == {
            "ok": True
        }
        assert core.security.secrets.get(name) == "pw"

        with pytest.raises(IpcError):
            await _call(core, "secrets.delete", {"name": name})
        assert await _call(core, "secrets.delete", {"name": name, "pin": "4711"}) == {"ok": True}
        assert core.security.secrets.get(name) is None
    finally:
        await runtime.stop()


async def test_the_registry_rejects_a_plugin_calling_a_settings_request(
    core: FakeCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOX_USER_CONFIG", str(tmp_path / "user.yaml"))
    runtime = install(core)
    try:
        from nox.ipc.dispatch import RequestContext

        envelope = Envelope(
            kind=Kind.REQUEST,
            name="config.set",
            src=Source(role="plugin", id="twitch"),
            payload={"values": {}},
        )
        ctx = RequestContext(client_id="twitch", role="plugin", request=envelope)
        response = await core.registry.dispatch(ctx, envelope)
        assert response.kind is Kind.ERROR
        assert response.payload["code"] == ERR_PERMISSION
    finally:
        await runtime.stop()
