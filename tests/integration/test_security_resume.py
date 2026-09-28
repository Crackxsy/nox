"""Resume from safe mode, against a real core.

The PIN is required only after a security-path kill - panic, tamper, a broken audit chain. A
user-initiated kill resumes on an explicit action, and only the user-controlled roles (shell,
dashboard, supervisor) may ask at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nox.app import PROFILES_DIR, NoxCore
from nox.core.state import PrivacyMode
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.handlers.core import CoreHandlers, PrivacySet, SecurityKill, SecurityResume
from nox.ipc.protocol import Envelope, Kind, Source
from nox.security.gate import SecurityChangeGate
from nox.security.secrets import InMemorySecretStore, PinManager
from tests.integration.test_walking_skeleton import _config

PIN = "270712"


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[NoxCore]:
    cfg = _config(tmp_path)
    instance = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
    await asyncio.wait_for(instance.start(), timeout=60)
    # never touch the real Windows Credential Manager in tests
    instance.security.secrets = InMemorySecretStore()
    instance.security.pin = PinManager(
        instance.security.secrets, audit=instance.security.audit, prefer_argon2=False
    )
    instance.security.gate = SecurityChangeGate(
        instance.security.pin, required=True, audit=instance.security.audit
    )
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=30)


def _handlers(core: NoxCore) -> CoreHandlers:
    """The core's own request handlers, called directly instead of over a socket."""
    return CoreHandlers(core)


def _audit_entries(core: NoxCore) -> list:  # type: ignore[type-arg]
    """Every audit row, with the queued writer drained first.

    Audit writes are handed to a background thread so a permission check never waits on the disk;
    a test that reads them back has to wait for that thread once.
    """
    assert core.security is not None
    core.security.audit.flush()
    return core.security.audit_store.entries()


def _ctx(role: str) -> RequestContext:
    envelope = Envelope(
        kind=Kind.REQUEST, name="security.resume", src=Source(role=role, id=f"test-{role}")
    )
    return RequestContext(client_id=f"c-{role}", role=role, request=envelope)


async def test_user_kill_resumes_without_a_pin(core: NoxCore) -> None:
    core.security.pin.set_pin(PIN, by="test")  # a configured PIN must not gate a user kill
    await core.security.killswitch.engage("ui", "user stop")
    assert core.security.killswitch.is_engaged()

    result = await _handlers(core).resume(_ctx("dashboard"), SecurityResume())

    # No supervisor in this test: the core runs standalone, and says so.
    assert result == {"ok": True, "supervisor": "standalone"}
    assert not core.security.killswitch.is_engaged()
    entry = next(e for e in _audit_entries(core) if e.action == "kill_switch.resume")
    assert entry.result == "ok"
    assert core.security.audit_store.details(entry.seq) == {
        "security_path": "false",
        "origin": "ui",
    }


async def test_panic_needs_the_pin_to_resume(core: NoxCore) -> None:
    core.security.pin.set_pin(PIN, by="test")
    await core.security.killswitch.panic(by="hotkey")
    assert core.security.killswitch.security_path is True

    # A refusal says why, so the dashboard can ask for the PIN instead of showing a bare "no".
    assert await _handlers(core).resume(_ctx("shell"), SecurityResume()) == {
        "ok": False,
        "reason": "pin_required",
    }
    assert await _handlers(core).resume(_ctx("shell"), SecurityResume(pin="000000")) == {
        "ok": False,
        "reason": "pin_wrong",
        "remaining_attempts": 4,
    }
    assert core.security.killswitch.is_engaged()

    assert await _handlers(core).resume(_ctx("shell"), SecurityResume(pin=PIN)) == {
        "ok": True,
        "supervisor": "standalone",
    }
    assert not core.security.killswitch.is_engaged()
    denied = [
        e for e in _audit_entries(core) if e.action == "kill_switch.resume" and e.result == "denied"
    ]
    assert len(denied) == 2
    assert core.security.audit_store.details(denied[0].seq)["security_path"] == "true"


async def test_security_path_kill_without_a_configured_pin_still_resumes_explicitly(
    core: NoxCore,
) -> None:
    """Fresh install: no PIN exists, so the explicit request counts and is audited as such."""
    assert not core.security.pin.is_set()
    await core.security.killswitch.engage("tamper", "hard prohibition touched")

    assert await _handlers(core).resume(_ctx("supervisor"), SecurityResume()) == {
        "ok": True,
        "supervisor": "standalone",
    }
    entry = next(e for e in _audit_entries(core) if e.action == "kill_switch.resume")
    assert core.security.audit_store.details(entry.seq) == {
        "security_path": "true",
        "origin": "tamper",
    }


@pytest.mark.parametrize("role", ["worker", "plugin", "pet", "remote"])
async def test_resume_from_a_non_user_role_is_rejected(core: NoxCore, role: str) -> None:
    await core.security.killswitch.engage("ui", "user stop")

    with pytest.raises(IpcError) as exc:
        await _handlers(core).resume(_ctx(role), SecurityResume())

    assert exc.value.code == ERR_PERMISSION
    assert core.security.killswitch.is_engaged()
    assert not [e for e in _audit_entries(core) if e.action == "kill_switch.resume"]


def test_only_user_roles_are_registered_for_security_resume(core: NoxCore) -> None:
    registration = core.registry.get("security.resume")
    assert registration is not None
    assert registration.allowed_roles == frozenset({"shell", "dashboard", "supervisor"})


async def test_ui_supplied_kill_origin_cannot_become_a_security_path_kill(core: NoxCore) -> None:
    """A dashboard may not name a security-path origin; the kill is recorded as its own role."""

    core.security.pin.set_pin(PIN, by="test")
    result = await _handlers(core).kill(
        _ctx("dashboard"), SecurityKill(origin="tamper", reason="spoof")
    )
    assert result["ok"] is True
    assert result["report"]["security_path"] is False
    assert core.security.killswitch.origin == "dashboard"
    # ... and therefore resumes without the PIN, as a user kill should
    assert (await _handlers(core).resume(_ctx("dashboard"), SecurityResume()))["ok"] is True


async def test_a_locked_pin_says_until_when(core: NoxCore) -> None:
    core.security.pin.set_pin(PIN, by="test")
    await core.security.killswitch.panic(by="hotkey")
    for _ in range(4):
        await _handlers(core).resume(_ctx("dashboard"), SecurityResume(pin="000000"))

    locked = await _handlers(core).resume(_ctx("dashboard"), SecurityResume(pin="000000"))

    assert locked["ok"] is False and locked["reason"] == "locked"
    assert locked["locked_until"]
    right_but_locked = await _handlers(core).resume(_ctx("dashboard"), SecurityResume(pin=PIN))
    assert right_but_locked["reason"] == "locked"
    assert core.security.killswitch.is_engaged()


async def test_resume_tells_the_supervisor_only_after_the_kill_switch_let_go(
    core: NoxCore,
) -> None:
    """A hotkey kill holds the supervisor in safe mode too; resuming must re-arm it - and a
    refused resume must not."""

    class FakeSupervisor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def notify_resumed(self, *, by: str) -> str:
            self.calls.append(by)
            return "rearmed"

    supervisor = FakeSupervisor()
    core.supervisor = supervisor  # type: ignore[assignment]
    core.security.pin.set_pin(PIN, by="test")
    await core.security.killswitch.panic(by="hotkey")

    assert (await _handlers(core).resume(_ctx("dashboard"), SecurityResume()))["ok"] is False
    assert supervisor.calls == []

    result = await _handlers(core).resume(_ctx("dashboard"), SecurityResume(pin=PIN))
    assert result == {"ok": True, "supervisor": "rearmed"}
    assert supervisor.calls == ["dashboard"]
    core.supervisor = None


# ---- privacy mode FULL from the dashboard -------------------------------------------------------


async def test_full_privacy_without_confirmation_says_it_was_not_applied(core: NoxCore) -> None:
    """The dashboard used to send `{mode: full}` and read the unchanged state as success."""
    handlers = _handlers(core)
    before = core.security.privacy.mode

    unconfirmed = await handlers.privacy_set(_ctx("dashboard"), PrivacySet(mode=PrivacyMode.FULL))
    assert unconfirmed["applied"] is False and unconfirmed["requires_confirmation"] is True
    assert unconfirmed["mode"] == before.value

    confirmed = await handlers.privacy_set(
        _ctx("dashboard"), PrivacySet(mode=PrivacyMode.FULL, confirmed=True)
    )
    assert confirmed["applied"] is True and confirmed["mode"] == "full"


async def test_relaxing_privacy_with_a_pin_set_refuses_with_a_reason(core: NoxCore) -> None:
    core.security.pin.set_pin(PIN, by="test")
    handlers = _handlers(core)
    relax = PrivacySet(mode=PrivacyMode.FULL, confirmed=True)

    with pytest.raises(IpcError) as missing:
        await handlers.privacy_set(_ctx("dashboard"), relax)
    assert missing.value.code == ERR_PERMISSION
    assert missing.value.details == {"reason": "pin_required"}

    with pytest.raises(IpcError) as wrong:
        await handlers.privacy_set(_ctx("dashboard"), relax.model_copy(update={"pin": "000000"}))
    assert wrong.value.details == {"reason": "pin_wrong", "remaining_attempts": 4}

    applied = await handlers.privacy_set(_ctx("dashboard"), relax.model_copy(update={"pin": PIN}))
    assert applied["mode"] == "full" and applied["applied"] is True
