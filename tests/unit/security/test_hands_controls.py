"""Negative tests for the Wave 1 hands controls: cmd grants, rememberable, taint, curated shell.

Each test is written so it FAILS if the control it covers is removed - a remembered `git status`
must not cover `git push`, a tainted turn must not honour a HIGH grant, a non-rememberable tool
must never produce a grant, and a curated read-only command must run without a confirmation.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from nox.security.model import Decision, PermissionRequest, Risk, TemporaryGrant
from nox.security.permissions import DefaultPermissionEngine

from .conftest import MutableClock


def _shell_req(target: str, **kw: object) -> PermissionRequest:
    base: dict[str, object] = {
        "agent": "companion",
        "tool": "shell",
        "action": "run",
        "mode": "companion",
        "risk": Risk.HIGH,
        "target": target,
    }
    base.update(kw)
    return PermissionRequest.model_validate(base)


# ---- cmd: grants ---------------------------------------------------------------------------------


def test_cmd_grant_matches_only_its_pattern(
    engine: DefaultPermissionEngine, clock: MutableClock
) -> None:
    engine.grant_temporary(
        TemporaryGrant(
            grant_id="g",
            scope="cmd:git status",
            max_risk=Risk.HIGH,
            expires_at=clock.now + timedelta(hours=1),
        )
    )
    assert engine.check(_shell_req("cmd:git status")).decision is Decision.ALLOW
    # The negative half: the same grant must NOT cover a different command pattern.
    assert engine.check(_shell_req("cmd:git push")).decision is Decision.CONFIRM


def test_cmd_grant_never_covers_a_complex_command(
    engine: DefaultPermissionEngine, clock: MutableClock
) -> None:
    engine.grant_temporary(
        TemporaryGrant(
            grant_id="g",
            scope="cmd:git status",
            max_risk=Risk.HIGH,
            expires_at=clock.now + timedelta(hours=1),
        )
    )
    # A command with a shell operator is encoded `complex:...` and can never equal a cmd grant.
    assert engine.check(_shell_req("complex:deadbeef")).decision is Decision.CONFIRM


def test_curated_readonly_runs_without_confirmation(engine: DefaultPermissionEngine) -> None:
    result = engine.check(_shell_req("readonly:git status"))
    assert result.decision is Decision.ALLOW
    assert result.rule_id == "shell.curated_readonly"
    # A non-curated simple command is HIGH -> CONFIRM, proving the allowance is not blanket.
    assert engine.check(_shell_req("cmd:git push")).decision is Decision.CONFIRM


def test_curated_readonly_is_shell_only(engine: DefaultPermissionEngine) -> None:
    # Another tool cannot borrow the readonly allowance by spelling the prefix in its target.
    other = PermissionRequest.model_validate(
        {
            "agent": "companion",
            "tool": "web",
            "action": "fetch",
            "mode": "companion",
            "risk": Risk.HIGH,
            "target": "readonly:git status",
        }
    )
    assert engine.check(other).decision is not Decision.ALLOW


# ---- taint guard ---------------------------------------------------------------------------------


def test_taint_drops_high_grant_to_confirmation(
    engine: DefaultPermissionEngine, clock: MutableClock
) -> None:
    engine.grant_temporary(
        TemporaryGrant(
            grant_id="g",
            scope="cmd:git push",
            max_risk=Risk.HIGH,
            expires_at=clock.now + timedelta(hours=1),
        )
    )
    assert engine.check(_shell_req("cmd:git push")).decision is Decision.ALLOW
    engine.set_tainted(True)
    # With untrusted content in the turn the HIGH grant must no longer act on its own.
    assert engine.check(_shell_req("cmd:git push")).decision is Decision.CONFIRM
    engine.set_tainted(False)
    assert engine.check(_shell_req("cmd:git push")).decision is Decision.ALLOW


def test_taint_keeps_low_grants(clock: MutableClock) -> None:
    """A LOW grant is load-bearing only when a profile rule would otherwise deny the tool; under
    taint it must still apply (the fallback is for grants *above* LOW), which pins `> LOW` vs
    `>= LOW`."""
    from nox.security.model import Profile, ProfileRule
    from nox.security.permissions import InMemoryGrantStore, PrivacySnapshot

    class _Profiles:
        def get(self, _id: str) -> Profile:
            return Profile(
                id="companion",
                rules=[ProfileRule(id="deny-notes", tool="notes", decision=Decision.DENY)],
            )

    class _Privacy:
        def snapshot(self) -> PrivacySnapshot:
            return PrivacySnapshot()

    engine = DefaultPermissionEngine(
        profiles=_Profiles(),  # type: ignore[arg-type]
        privacy=_Privacy(),
        grants=InMemoryGrantStore(),
        clock=lambda: clock.now,
    )
    engine.grant_temporary(
        TemporaryGrant(
            grant_id="g",
            scope="session:notes/*",
            max_risk=Risk.LOW,
            expires_at=clock.now + timedelta(hours=1),
        )
    )
    low = PermissionRequest.model_validate(
        {
            "agent": "companion",
            "tool": "notes",
            "action": "read",
            "mode": "companion",
            "risk": Risk.LOW,
        }
    )
    assert engine.check(low).decision is Decision.ALLOW  # grant beats the profile deny
    engine.set_tainted(True)
    # Taint drops only grants above LOW; this LOW grant must survive, so the deny must not win.
    assert engine.check(low).decision is Decision.ALLOW


# ---- rememberable --------------------------------------------------------------------------------


def test_non_rememberable_confirm_creates_no_grant(engine: DefaultPermissionEngine) -> None:
    async def scenario() -> None:
        request = PermissionRequest.model_validate(
            {
                "agent": "companion",
                "tool": "package",
                "action": "install",
                "mode": "companion",
                "risk": Risk.HIGH,
                "target": "VideoLAN.VLC",
                "rememberable": False,
            }
        )
        grant_id = engine.request_confirmation(request)
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.decision is Decision.ALLOW
        assert result.grant_id is None  # remember was ignored for a non-rememberable tool

    asyncio.run(scenario())


def test_shell_remember_creates_cmd_grant_not_session(engine: DefaultPermissionEngine) -> None:
    async def scenario() -> None:
        request = _shell_req("cmd:git push")
        grant_id = engine.request_confirmation(request)
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.grant_id is not None
        # The remembered grant is the command pattern, so it covers `git push` and nothing else.
        assert engine.check(_shell_req("cmd:git push")).decision is Decision.ALLOW
        assert engine.check(_shell_req("cmd:git status")).rule_id != "grant.cmd:git push"

    asyncio.run(scenario())


def test_shell_remember_ignored_for_complex(engine: DefaultPermissionEngine) -> None:
    async def scenario() -> None:
        request = _shell_req("complex:abc123")
        grant_id = engine.request_confirmation(request)
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.grant_id is None  # a complex command can never be remembered

    asyncio.run(scenario())
