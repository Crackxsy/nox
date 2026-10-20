"""Negative tests for the Wave 1 hands controls: cmd grants, rememberable, taint, curated shell.

Each test is written so it FAILS if the control it covers is removed - a remembered `git status`
must not cover `git push`, a tainted turn must not honour a HIGH grant, a non-rememberable tool
must never produce a grant, and a curated read-only command must run without a confirmation.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from nox.hands.shell import ProgramResolver, classify
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
    # In the companion profile a dedicated rule allows readonly:*; either that rule or the
    # fallback guard answers ALLOW, both named for the read-only case.
    result = engine.check(_shell_req("readonly:git status"))
    assert result.decision is Decision.ALLOW
    assert "readonly" in result.rule_id
    # A non-curated simple command is HIGH -> CONFIRM, proving the allowance is not blanket.
    assert engine.check(_shell_req("cmd:git push")).decision is Decision.CONFIRM


def test_curated_readonly_guard_lifts_default_without_a_profile_rule(clock: MutableClock) -> None:
    """A profile with no shell rule: the fallback guard lifts the HIGH default for readonly only."""
    from nox.security.model import Profile

    engine = _engine_with_profile(Profile(id="bare", rules=[]), clock)
    assert engine.check(_shell_req("readonly:df")).rule_id == "shell.curated_readonly"
    assert engine.check(_shell_req("readonly:df")).decision is Decision.ALLOW
    # A non-readonly shell command still hits the HIGH default (confirm).
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


def _engine_with_profile(profile, clock):  # noqa: ANN001
    from nox.security.permissions import (
        DefaultPermissionEngine,
        InMemoryGrantStore,
        PrivacySnapshot,
    )

    class _Profiles:
        def get(self, _id: str):  # noqa: ANN202
            return profile

    class _Privacy:
        def snapshot(self) -> PrivacySnapshot:
            return PrivacySnapshot()

    return DefaultPermissionEngine(
        profiles=_Profiles(),  # type: ignore[arg-type]
        privacy=_Privacy(),
        grants=InMemoryGrantStore(),
        clock=lambda: clock.now,
    )


def test_profile_deny_beats_curated_readonly(clock: MutableClock) -> None:
    """A profile that denies shell must deny even a curated read-only command. The read-only
    exception may lift only the HIGH default, never an explicit profile DENY (guard ordering)."""
    from nox.security.model import Profile, ProfileRule

    profile = Profile(
        id="locked",
        rules=[ProfileRule(id="no-shell", tool="shell", decision=Decision.DENY)],
    )
    engine = _engine_with_profile(profile, clock)
    assert engine.check(_shell_req("readonly:df")).decision is Decision.DENY


def test_profile_confirm_beats_curated_readonly(clock: MutableClock) -> None:
    from nox.security.model import Profile, ProfileRule

    profile = Profile(
        id="ask",
        rules=[ProfileRule(id="ask-shell", tool="shell", decision=Decision.CONFIRM)],
    )
    engine = _engine_with_profile(profile, clock)
    assert engine.check(_shell_req("readonly:df")).decision is Decision.CONFIRM


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
    req = _shell_req("cmd:git push", conversation="c1")
    assert engine.check(req).decision is Decision.ALLOW
    engine.mark_tainted("c1")
    # With untrusted content in the conversation the HIGH grant must no longer act on its own.
    assert engine.check(req).decision is Decision.CONFIRM
    # Another conversation is not affected ...
    assert engine.check(_shell_req("cmd:git push", conversation="c2")).decision is Decision.ALLOW
    # ... and only a reset of the conversation's context clears it.
    engine.clear_taint("c1")
    assert engine.check(req).decision is Decision.ALLOW


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
            "conversation": "c1",
        }
    )
    assert engine.check(low).decision is Decision.ALLOW  # grant beats the profile deny
    engine.mark_tainted("c1")
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


def test_shell_remember_never_applies_to_a_hand_written_cmd_target(
    engine: DefaultPermissionEngine,
) -> None:
    async def scenario() -> None:
        # `git` is never SIMPLE, so `shell.run` never produces `cmd:git push`; a target without
        # the resolved path and digest is not a SIMPLE command and stays never-remembered.
        request = _shell_req("cmd:git push")
        assert engine._remember_scope(request) == "cmd:git push"
        grant_id = engine.request_confirmation(request)
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.decision is Decision.ALLOW
        assert result.grant_id is None
        assert engine.check(_shell_req("cmd:git push")).decision is Decision.CONFIRM

    asyncio.run(scenario())


#: What `shell.run` produces for a SIMPLE argv such as `["whoami", "/all"]`.
SIMPLE_TARGET = "cmd:/usr/bin/whoami#0123456789abcdef /all"


def test_a_simple_shell_command_is_remembered_for_the_session_only(
    engine: DefaultPermissionEngine,
) -> None:
    async def scenario() -> None:
        grant_id = engine.request_confirmation(_shell_req(SIMPLE_TARGET))
        (pending,) = [p for p in engine.pending_requests() if p.request_id == grant_id]
        assert pending.remember_ttl_s > 0  # the prompt offers "remember"
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.grant_id is not None
        # Exactly this command runs without a question now ...
        assert engine.check(_shell_req(SIMPLE_TARGET)).decision is Decision.ALLOW
        # ... but never a different subcommand, a different binary or the same one with egress.
        for other in (
            "cmd:/usr/bin/whoami#0123456789abcdef /priv",
            "cmd:/usr/bin/whoami#fedcba9876543210 /all",
            SIMPLE_TARGET + " egress=a.example",
            "complex:0123456789abcdef",
        ):
            assert engine.check(_shell_req(other)).decision is Decision.CONFIRM, other
        # The grant ends with the session.
        assert engine.end_session() >= 1
        assert engine.check(_shell_req(SIMPLE_TARGET)).decision is Decision.CONFIRM

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target,overrides",
    [
        ("cmd:git push", {}),  # no resolved path + digest: not produced by the SIMPLE classifier
        ("cmd:/usr/bin/git#0123456789abcdef push", {"risk": Risk.CRITICAL}),
        (SIMPLE_TARGET + " egress=a.example", {}),  # a declared network is always asked
        (SIMPLE_TARGET, {"rememberable": False}),
        (SIMPLE_TARGET, {"tool": "files"}),
        ("complex:0123456789abcdef", {}),
        ("readonly:whoami", {}),
        ("cmd:/usr/bin/whoami#0123456789ABCDEF", {}),  # digest must be lowercase hex
        ("cmd:/usr/bin/whoami#0123456789abcdef a b", {}),  # one word at most
    ],
)
def test_nothing_but_a_simple_shell_command_is_rememberable_at_high_risk(
    engine: DefaultPermissionEngine, target: str, overrides: dict[str, object]
) -> None:
    assert not engine._can_remember(_shell_req(target, **overrides))


def test_the_real_classifier_output_is_what_may_be_remembered(
    engine: DefaultPermissionEngine, tmp_path: Path
) -> None:
    exe = ".exe" if sys.platform == "win32" else ""
    for name in ("whoami", "git", "python", "df"):
        (tmp_path / f"{name}{exe}").write_bytes(name.encode())
    resolver = ProgramResolver(dirs=(str(tmp_path),))

    def rememberable(argv: list[str], egress: tuple[str, ...] = ()) -> bool:
        info = classify(argv=argv, egress=egress, resolver=resolver)
        return engine._can_remember(_shell_req(info.target))

    assert rememberable(["whoami", "/all"])
    assert rememberable(["df", "-h"])
    assert not rememberable(["whoami", "/all"], egress=("a.example",))
    assert not rememberable(["git", "status"])
    assert not rememberable(["python", "-c", "print(1)"])
    assert not rememberable(["df", "--hum"])  # a GNU abbreviation is not on the allowlist
    assert not engine._can_remember(_shell_req(classify(command="whoami /all").target))


def test_shell_remember_ignored_for_complex(engine: DefaultPermissionEngine) -> None:
    async def scenario() -> None:
        request = _shell_req("complex:abc123")
        grant_id = engine.request_confirmation(request)
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.grant_id is None  # a complex command can never be remembered

    asyncio.run(scenario())


def test_a_prompt_offers_remember_only_when_it_would_create_a_grant(
    engine: DefaultPermissionEngine,
) -> None:
    def request(**kw: object) -> PermissionRequest:
        base: dict[str, object] = {"agent": "companion", "mode": "companion", "target": "x"}
        return PermissionRequest.model_validate({**base, **kw})

    async def scenario() -> None:
        def ttl(req: PermissionRequest) -> float:
            grant_id = engine.request_confirmation(req)
            (pending,) = [p for p in engine.pending_requests() if p.request_id == grant_id]
            return pending.remember_ttl_s

        # High risk is asked every time, and a complex command or a non-rememberable tool never
        # produces a grant: the prompt must not offer "remember" for any of them.
        assert ttl(_shell_req("cmd:git push")) == 0
        assert ttl(_shell_req(SIMPLE_TARGET)) > 0
        assert ttl(_shell_req("complex:abc123")) == 0
        not_rememberable = request(
            tool="package", action="list", risk=Risk.MEDIUM, rememberable=False
        )
        assert ttl(not_rememberable) == 0
        # A rememberable medium-risk request offers it, for the session grant's lifetime.
        assert ttl(request(tool="files", action="read", risk=Risk.MEDIUM)) > 0

    asyncio.run(scenario())
