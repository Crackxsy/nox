"""Mobile Companion integration (EPIC-17, Spec v0.8 §9/§10): boots the real `NoxCore` headless,
calls `nox.remote.install.install(core)` the way the integrator will, registers a test double for
the telegram plugin's `telegram.send` tool on the real `ToolRegistry`, and drives a full pairing
round trip plus the security-relevant commands over the real event bus.

Nothing here is mocked except the transport tool: the permission engine, the `ToolExecutor`, the
hash-chained audit log, the kill switch and the privacy service are the production objects, so a
regression in any of them shows up as a failure in this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.events import E, Event
from nox.core.state import PrivacyMode
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import IpcError
from nox.ipc.protocol import Envelope, Kind, Source
from nox.remote.install import install
from nox.security.model import Risk
from nox.tools.registry import ToolSpec
from tests._ports import free_port_base

SENDER = "987654321"
STRANGER = "111222333"


def _config(tmp_path: Path, **overrides: Any):
    base = free_port_base()
    settings = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(tmp_path / "vault"),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "remote": {"enabled": True, "notifications": {"events": ["security.kill_switch"]}},
        **overrides,
    }
    (tmp_path / "vault").mkdir()
    return load_config(DEFAULTS_PATH, None, None, settings)


class _SendInput(BaseModel):
    text: str
    chat_id: str = ""


@pytest.fixture
async def core(tmp_path: Path):
    core = NoxCore(_config(tmp_path), voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await core.start()
    try:
        yield core
    finally:
        await core.stop()


@pytest.fixture
def sent(core: NoxCore) -> list[dict[str, Any]]:
    """Test double for the telegram plugin's tool, on the real registry: the core reaches the phone
    only through the permission-checked, audited tool pipeline."""
    calls: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"sent": True}

    core.tool_registry.register(
        ToolSpec(
            name="telegram.send",
            description="test double for the telegram plugin's send tool",
            input_model=_SendInput,
            risk=Risk.LOW,
            handler=handler,
        )
    )
    install(core)
    return calls


def _ctx(role: str = "dashboard") -> RequestContext:
    return RequestContext(
        client_id="test",
        role=role,
        request=Envelope(
            kind=Kind.REQUEST, name="remote.pair.start", src=Source(role=role, id=role)
        ),
    )


async def _call(core: NoxCore, name: str, payload: dict[str, Any], *, role: str = "dashboard"):
    registration = core.registry.get(name)
    assert registration is not None, f"{name} is not registered"
    assert registration.allowed_roles is None or role in registration.allowed_roles
    model = registration.payload_model.model_validate(payload)
    return await registration.handler(_ctx(role), model)


async def _message(core: NoxCore, text: str, *, sender_id: str = SENDER, update_id: int = 1):
    await core.bus.publish(
        Event(
            name=E.REMOTE_MESSAGE,
            payload={
                "channel": "telegram",
                "sender_id": sender_id,
                "chat_id": "77",
                "update_id": update_id,
                "text": text,
            },
        )
    )


async def _pair(core: NoxCore, *, sender_id: str = SENDER, update_id: int = 1) -> str:
    start = await _call(core, "remote.pair.start", {"name": "Pixel"})
    await _message(core, f"/pair {start['code']}", sender_id=sender_id, update_id=update_id)
    devices = await _call(core, "remote.devices.list", {})
    return str(devices["devices"][0]["id"])


# -- pairing round trip ---------------------------------------------------------------------------


async def test_full_pairing_round_trip(core: NoxCore, sent: list[dict[str, Any]]):
    device_id = await _pair(core)

    devices = await _call(core, "remote.devices.list", {})
    assert devices["enabled"] is True
    assert devices["devices"][0]["name"] == "Pixel"
    assert devices["devices"][0]["revoked_at"] is None
    assert "public_key" not in devices["devices"][0]
    assert "Gekoppelt" in sent[-1]["text"]
    assert device_id


async def test_the_remote_ipc_surface_is_local_only(core: NoxCore, sent: list[dict[str, Any]]):
    """A phone cannot pair a second device or revoke one: the `remote` role holds none of these."""
    for name in ("remote.pair.start", "remote.devices.list", "remote.unpair"):
        registration = core.registry.get(name)
        assert registration is not None
        assert registration.allowed_roles == frozenset({"shell", "dashboard"})


async def test_status_from_the_phone_is_answered_through_the_tool_pipeline(
    core: NoxCore, sent: list[dict[str, Any]]
):
    await _pair(core)
    sent.clear()

    await _message(core, "/status", update_id=2)

    assert "Modus" in sent[-1]["text"]
    for forbidden in ("transcript", "memory", "prompt"):
        assert forbidden not in sent[-1]["text"].lower()
    # The audit log writes on its own thread; flush it, then read the same chain directly.
    audit = core.security.audit
    flush = getattr(audit, "flush", None)
    if flush is not None:
        flush(timeout_s=5.0)
    entries = getattr(audit, "inner", audit).entries(limit=200)
    audited = [e for e in entries if e.tool == "telegram"]
    assert audited and all(e.decision == "allow" for e in audited)


# -- security -----------------------------------------------------------------------------------


async def test_an_unpaired_sender_is_ignored_and_audited(core: NoxCore, sent: list[dict[str, Any]]):
    await _message(core, "/kill", sender_id=STRANGER)

    assert sent == []
    assert not core.security.killswitch.is_engaged()
    rows = core.db.fetch_all("SELECT command, decision, reason FROM remote_audit")
    assert [(r["command"], r["decision"], r["reason"]) for r in rows] == [
        ("kill", "deny", "not_paired")
    ]


async def test_kill_from_the_phone_engages_and_resumes_locally_without_a_pin(
    core: NoxCore, sent: list[dict[str, Any]]
):
    await _pair(core)

    await _message(core, "/kill", update_id=2)

    killswitch = core.security.killswitch
    assert killswitch.is_engaged()
    assert killswitch.security_path is False  # a user origin, not a security-path kill
    assert await killswitch.resume(pin_ok=True, by="dashboard") is True


async def test_resume_is_refused_from_the_phone_and_from_the_remote_role(
    core: NoxCore, sent: list[dict[str, Any]]
):
    """Resume stays local (Security Model §6). Note what the reply list shows: once safe mode is
    engaged the permission engine denies every side-effecting tool, `telegram.send` included, so
    the phone is not even told "no" - the kill switch really does stop everything. That is the
    conservative behaviour, not a bug, and it is deliberately *not* carved out for `remote`
    (ST-17-07: no remote-specific exception anywhere in the permission path)."""
    await _pair(core)
    await _message(core, "/kill", update_id=2)
    sent.clear()

    await _message(core, "/resume", update_id=3)

    assert core.security.killswitch.is_engaged()
    assert sent == []  # safe mode denies the outbound tool; nothing is faked as delivered
    rows = core.db.fetch_all(
        "SELECT command, decision, reason FROM remote_audit WHERE command = ?", ("resume",)
    )
    assert [(r["decision"], r["reason"]) for r in rows] == [("deny", "resume_not_remote")]
    # And the IPC path is closed too: `security.resume` never lists `remote` among its roles.
    registration = core.registry.get("security.resume")
    assert registration is not None and registration.allowed_roles is not None
    assert "remote" not in registration.allowed_roles


async def test_privacy_downgrade_allowed_upgrade_denied(core: NoxCore, sent: list[dict[str, Any]]):
    await _pair(core)

    sent.clear()

    await _message(core, "/privacy offline", update_id=2)
    assert core.security.privacy.mode is PrivacyMode.OFFLINE

    await _message(core, "/privacy full", update_id=3)
    assert core.security.privacy.mode is PrivacyMode.OFFLINE
    rows = core.db.fetch_all(
        "SELECT decision, reason FROM remote_audit WHERE command = ? ORDER BY id", ("privacy",)
    )
    assert [(r["decision"], r["reason"]) for r in rows] == [
        ("allow", ""),
        ("deny", "privacy_upgrade_denied"),
    ]
    # In OFFLINE the egress-bound `telegram.send` is denied like any other cloud tool, so the
    # refusal cannot be delivered either - the mode the phone asked for is the mode that silences
    # it. Honest over convenient (Security Model §5).
    assert sent == []


async def test_revocation_takes_effect_on_the_next_message(
    core: NoxCore, sent: list[dict[str, Any]]
):
    device_id = await _pair(core)

    result = await _call(core, "remote.unpair", {"device_id": device_id})
    assert result["ok"] is True
    sent.clear()

    await _message(core, "/status", update_id=2)

    assert sent == []  # a revoked device is an unpaired sender: ignored and audited
    devices = await _call(core, "remote.devices.list", {})
    assert devices["devices"][0]["revoked_at"] is not None


async def test_replayed_message_is_rejected(core: NoxCore, sent: list[dict[str, Any]]):
    await _pair(core)
    await _message(core, "/status", update_id=7)
    sent.clear()

    await _message(core, "/kill", update_id=7)

    assert not core.security.killswitch.is_engaged()
    assert "bereits verarbeitet" in sent[-1]["text"]


async def test_unknown_device_cannot_be_revoked(core: NoxCore, sent: list[dict[str, Any]]):
    with pytest.raises(IpcError):
        await _call(core, "remote.unpair", {"device_id": "does-not-exist"})


# -- notifications ------------------------------------------------------------------------------


async def test_a_notification_reaches_the_phone_while_the_system_is_running(
    core: NoxCore, sent: list[dict[str, Any]]
):
    await _pair(core)
    sent.clear()

    await core.bus.publish(
        Event(name=E.SECURITY_KILL_SWITCH, payload={"by": "dashboard", "reason": "test"})
    )

    assert any("Not-Aus" in call["text"] for call in sent)


async def test_a_real_kill_silences_the_notification_channel_too(
    core: NoxCore, sent: list[dict[str, Any]]
):
    """The kill-switch event that a *real* kill emits arrives after safe mode is already on, so
    `telegram.send` is denied with it. FR-12.4 wants the phone told about a kill; the permission
    engine says no side effects in safe mode, and the permission engine wins. Open point for the
    PO (see the SP-14 note): either the phone stays uninformed after a kill, or a narrowly scoped
    notify-only exception is designed - which is a product decision, not one to make here."""
    await _pair(core)
    sent.clear()

    await core.security.killswitch.engage("dashboard", "test")

    assert core.security.killswitch.is_engaged()
    assert sent == []


async def test_disabled_remote_registers_the_surface_but_runs_nothing(tmp_path: Path):
    core = NoxCore(
        _config(tmp_path, remote={"enabled": False}),
        voice=False,
        profiles_dir=PROFILES_DIR,
        extensions=False,
    )
    await core.start()
    try:
        install(core)
        with pytest.raises(IpcError):
            await _call(core, "remote.pair.start", {"name": "Pixel"})
        await _message(core, "/status")
        assert core.db.fetch_all("SELECT * FROM remote_audit") == []
    finally:
        await core.stop()
