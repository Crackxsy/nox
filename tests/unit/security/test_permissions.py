"""Permission engine: evaluation order, hard list, privacy, grants, profiles, confirmation flow."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from nox.core.events import E
from nox.core.state import PrivacyMode
from nox.security.audit import SqliteAuditLog
from nox.security.model import (
    Decision,
    PermissionRequest,
    Profile,
    ProfileRule,
    Risk,
    TemporaryGrant,
)
from nox.security.permissions import DefaultPermissionEngine, PrivacySnapshot
from nox.security.privacy import PrivacyService
from nox.security.profiles import PROFILE_IDS
from tests.unit.fakes import FakeBus

from .conftest import MutableClock, req

# ---- hard prohibitions --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,action",
    [
        ("game.input", "send"),
        ("game.input.send", ""),
        ("stream", "stop"),
        ("stream.key", "read"),
        ("recording", "delete"),
        ("permission", "self_elevate"),
    ],
)
def test_hard_prohibitions_denied_regardless_of_everything(
    engine: DefaultPermissionEngine,
    tool: str,
    action: str,
    clock: MutableClock,
) -> None:
    engine.grant_temporary(
        TemporaryGrant(
            grant_id="g",
            scope=f"session:{tool}/*",
            max_risk=Risk.HIGH,
            expires_at=clock.now + timedelta(hours=1),
        )
    )
    for agent in ("nox.chat", "plugin:x", "system"):
        for origin in ("local", "voice", "telegram", "plugin"):
            r = engine.check(req(tool, action, Risk.READ, agent=agent, origin=origin))
            assert r.decision is Decision.DENY and r.rule_id.startswith("hard."), (
                tool,
                action,
                agent,
            )


def test_self_elevate_denied_in_every_profile(engine: DefaultPermissionEngine) -> None:
    for pid in PROFILE_IDS:
        engine.set_profile(pid, by="test")
        r = engine.check(req("permission", "self_elevate", Risk.READ, agent="nox.coding"))
        assert r.decision is Decision.DENY and r.rule_id == "hard.permission.self_elevate"


def test_unvalidated_profile_allowing_hard_entry_is_still_denied(
    privacy: PrivacyService,
    audit: SqliteAuditLog,
    clock: MutableClock,
) -> None:
    class RawProvider:
        def get(self, profile_id: str) -> Profile:
            return Profile.model_construct(
                id="raw", rules=[ProfileRule(id="raw.allow_all", decision=Decision.ALLOW)]
            )

        def available(self) -> list[str]:
            return ["raw"]

    engine = DefaultPermissionEngine(
        profiles=RawProvider(), privacy=privacy, audit=audit, clock=clock, initial_profile="raw"
    )
    assert engine.check(req("memory", "write", Risk.MEDIUM)).decision is Decision.ALLOW
    assert engine.check(req("game.input", "send", Risk.READ)).decision is Decision.DENY


# ---- safe mode / privacy ------------------------------------------------------------------------


def test_safe_mode_denies_side_effects_but_allows_reads(
    engine: DefaultPermissionEngine,
    safe_mode_flag: dict[str, bool],
) -> None:
    safe_mode_flag["on"] = True
    assert engine.check(req("memory", "write", Risk.LOW)).rule_id == "safe_mode"
    assert engine.check(req("state", "read", Risk.READ)).decision is Decision.ALLOW


async def test_private_mode_denies_cloud_tools(
    engine: DefaultPermissionEngine, privacy: PrivacyService
) -> None:
    assert engine.check(req("web", "search", Risk.READ)).decision is Decision.ALLOW
    await privacy.set_mode(PrivacyMode.PRIVATE, by="user")
    for tool, action in (
        ("web", "search"),
        ("cloud", "chat"),
        ("ai.cloud", "complete"),
        ("screenshot", "upload"),
    ):
        r = engine.check(req(tool, action, Risk.READ))
        assert r.decision is Decision.DENY and r.rule_id == "privacy.private.cloud", tool
    assert engine.check(req("memory", "write", Risk.LOW)).rule_id == "privacy.private.memory_write"
    assert engine.check(req("memory", "search", Risk.READ)).decision is Decision.ALLOW
    await privacy.set_mode(PrivacyMode.OFFLINE, by="user")
    assert engine.check(req("twitch", "chat.send", Risk.LOW)).rule_id == "privacy.offline.cloud"


async def test_zone_active_denies_capture_and_memory_write(
    engine: DefaultPermissionEngine,
    privacy: PrivacyService,
) -> None:
    assert engine.check(req("capture.screen", "grab", Risk.READ)).decision is Decision.ALLOW
    assert (
        await privacy.observe_foreground("KeePass - secrets.kdbx", "KeePass.exe")
        == "password_manager"
    )
    for tool in ("capture.screen", "screenshot", "clipboard", "camera", "microphone"):
        r = engine.check(req(tool, "grab", Risk.READ))
        assert r.decision is Decision.DENY and r.rule_id == "privacy.zone_active", tool
    assert engine.check(req("memory", "write", Risk.LOW)).rule_id == "privacy.zone_active"
    assert engine.check(req("state", "read", Risk.READ)).decision is Decision.ALLOW
    await privacy.observe_foreground("Visual Studio Code", "Code.exe")
    assert engine.check(req("capture.screen", "grab", Risk.READ)).decision is Decision.ALLOW


def test_critical_risk_is_denied_with_pin_flag(engine: DefaultPermissionEngine) -> None:
    r = engine.check(req("security", "config.write", Risk.CRITICAL))
    assert r.decision is Decision.DENY and r.requires_pin is True
    with pytest.raises(ValueError):
        engine.grant_temporary(
            TemporaryGrant(
                grant_id="c",
                scope="task:1",
                max_risk=Risk.CRITICAL,
                expires_at=engine._clock() + timedelta(hours=1),
            )
        )


# ---- grants -----------------------------------------------------------------------------------


def test_grant_allows_then_expires(engine: DefaultPermissionEngine, clock: MutableClock) -> None:
    request = req("filesystem", "write", Risk.MEDIUM, task_id="t1", target="D:/Projects/app/x.txt")
    assert engine.check(request).decision is Decision.CONFIRM
    engine.grant_temporary(
        TemporaryGrant(
            grant_id="g1",
            scope="task:t1",
            max_risk=Risk.MEDIUM,
            expires_at=clock.now + timedelta(seconds=60),
        )
    )
    r = engine.check(request)
    assert r.decision is Decision.ALLOW and r.grant_id == "g1"
    assert (
        engine.check(req("filesystem", "write", Risk.HIGH, task_id="t1")).decision
        is Decision.CONFIRM
    )
    assert (
        engine.check(req("filesystem", "write", Risk.MEDIUM, task_id="other")).decision
        is Decision.CONFIRM
    )
    clock.advance(61)
    assert engine.check(request).decision is Decision.CONFIRM
    engine.revoke("g1")


def test_repo_scoped_grant_matches_paths_case_insensitively(
    engine: DefaultPermissionEngine,
    clock: MutableClock,
) -> None:
    engine.grant_temporary(
        TemporaryGrant(
            grant_id="r",
            scope=r"repo:D:\Projects\app",
            max_risk=Risk.MEDIUM,
            expires_at=clock.now + timedelta(hours=1),
        )
    )
    assert (
        engine.check(
            req("filesystem", "write", Risk.MEDIUM, target="d:/projects/APP/a/b.py")
        ).decision
        is Decision.ALLOW
    )
    assert (
        engine.check(req("filesystem", "write", Risk.MEDIUM, target="D:/Projects/app2/x")).decision
        is Decision.CONFIRM
    )


# ---- profiles ---------------------------------------------------------------------------------


def test_work_profile_denies_memory_and_cloud(engine: DefaultPermissionEngine) -> None:
    engine.set_profile("work", by="user")
    assert engine.active_profile().id == "work"
    for tool, action in (("memory", "write"), ("vault", "append_inbox"), ("vault", "write")):
        r = engine.check(req(tool, action, Risk.LOW))
        assert r.decision is Decision.DENY and r.rule_id == "work.memory_writes_disabled", tool
    assert engine.check(req("cloud", "chat", Risk.READ)).rule_id == "work.cloud_disabled"
    assert engine.check(req("memory", "search", Risk.READ)).decision is Decision.ALLOW


def test_coding_profile_filesystem_roots(engine: DefaultPermissionEngine) -> None:
    engine.set_profile("coding", by="user")
    inside = req(
        "filesystem",
        "write",
        Risk.MEDIUM,
        agent="nox.coding",
        target=r"%USERPROFILE%\Projects\app\src\a.py",
    )
    outside = req(
        "filesystem", "read", Risk.READ, agent="nox.coding", target=r"C:\Windows\System32\x"
    )
    assert engine.check(inside).decision is Decision.ALLOW
    assert engine.check(outside).rule_id == "coding.filesystem_roots"
    assert (
        engine.check(req("git", "force_push", Risk.HIGH, agent="nox.coding")).decision
        is Decision.DENY
    )


def test_stream_profile_confirms_scene_switch(engine: DefaultPermissionEngine) -> None:
    engine.set_profile("stream", by="user")
    assert (
        engine.check(req("obs", "scene.switch", Risk.MEDIUM, mode="stream")).decision
        is Decision.CONFIRM
    )
    assert (
        engine.check(req("obs", "scene.delete", Risk.HIGH, mode="stream")).decision is Decision.DENY
    )
    assert (
        engine.check(req("screenshot", "upload", Risk.MEDIUM, target="https://x")).rule_id
        == "stream.screenshots_to_cloud"
    )


def test_default_by_risk_and_first_match_wins(engine: DefaultPermissionEngine) -> None:
    assert engine.check(req("time", "now", Risk.READ)).rule_id == "default.read"
    assert engine.check(req("pet", "express", Risk.LOW)).rule_id == "default.low"
    assert engine.check(req("obs", "scene.switch", Risk.MEDIUM)).decision is Decision.CONFIRM
    assert engine.check(req("git", "push", Risk.HIGH)).rule_id == "companion.git.push"
    assert engine.check(req("nothing", "x", Risk.CRITICAL)).decision is Decision.DENY


def test_set_profile_unknown_raises_and_keeps_current(engine: DefaultPermissionEngine) -> None:
    with pytest.raises(KeyError):
        engine.set_profile("ghost", by="user")
    assert engine.active_profile().id == "companion"


def test_evaluate_is_pure_and_deterministic(
    engine: DefaultPermissionEngine, clock: MutableClock
) -> None:
    snap = PrivacySnapshot(mode=PrivacyMode.BALANCED)
    r1 = engine.evaluate(
        req("obs", "scene.switch", Risk.MEDIUM), engine.active_profile(), snap, [], clock.now
    )
    r2 = engine.evaluate(
        req("obs", "scene.switch", Risk.MEDIUM), engine.active_profile(), snap, [], clock.now
    )
    assert r1 == r2


# ---- confirmation flow --------------------------------------------------------------------------


async def test_confirm_timeout_is_deny(engine: DefaultPermissionEngine, bus: FakeBus) -> None:
    r = await engine.authorize(req("obs", "scene.switch", Risk.MEDIUM))
    assert r.decision is Decision.DENY and r.rule_id == "confirm.timeout"
    names = bus.names()
    assert E.SECURITY_PERMISSION_REQUESTED in names
    decided = [e for e in bus.published if e.name == E.SECURITY_PERMISSION_DECIDED]
    assert decided[-1].payload["decision"] == "deny" and decided[-1].payload["by"] == "policy"
    assert engine.pending() == []


async def test_confirm_reply_allow_with_remember_creates_session_grant(
    engine: DefaultPermissionEngine,
    bus: FakeBus,
) -> None:
    request = req("obs", "scene.switch", Risk.MEDIUM)

    async def answer() -> None:
        await asyncio.sleep(0)
        gid = engine.pending()[0]
        assert engine.reply(gid, Decision.ALLOW, remember=True, by="user")
        assert not engine.reply(gid, Decision.DENY)  # second reply is ignored

    task = asyncio.create_task(answer())
    r = await engine.authorize(request)
    await task
    assert r.decision is Decision.ALLOW and r.rule_id == "confirm.user" and r.grant_id
    again = engine.check(request)
    assert again.decision is Decision.ALLOW and again.grant_id == r.grant_id
    assert engine.check(req("obs", "scene.delete", Risk.MEDIUM)).decision is Decision.CONFIRM
    assert engine.end_session() == 1
    assert engine.check(request).decision is Decision.CONFIRM
    decided = [e for e in bus.published if e.name == E.SECURITY_PERMISSION_DECIDED]
    assert decided[-1].payload["by"] == "user"


async def test_confirm_reply_deny(engine: DefaultPermissionEngine) -> None:
    gid = engine.request_confirmation(req("obs", "scene.switch", Risk.MEDIUM))
    engine.reply(gid, Decision.DENY, remember=True)
    r = await engine.await_confirmation(gid)
    assert r.decision is Decision.DENY and r.rule_id == "confirm.denied" and r.grant_id is None
    assert not engine.reply("unknown", Decision.ALLOW)


def test_every_decision_is_audited(engine: DefaultPermissionEngine, audit: SqliteAuditLog) -> None:
    before = audit.count()
    engine.check(req("game.input", "send", Risk.READ, task_id="t9"))
    engine.check(req("time", "now", Risk.READ))
    entries = audit.entries(since_seq=before)
    assert [e.result for e in entries] == ["denied", "ok"]
    assert entries[0].task_id == "t9" and entries[0].tool == "game.input"
    assert audit.details(entries[0].seq)["rule_id"] == "hard.game.input.send"
    assert audit.verify_chain()


def test_request_model_is_frozen() -> None:
    r = PermissionRequest(agent="a", tool="t", action="x", mode="m", risk=Risk.LOW)
    with pytest.raises(Exception):  # noqa: B017 - pydantic frozen raises ValidationError
        r.tool = "other"  # type: ignore[misc]
