"""Remembered grants end with the session, and remembered shell commands end sooner (issue #138).

"Remember for this session" used to have no session end at all: `end_session()` existed but
nothing called it, so a remembered grant lived for its whole TTL across conversations. These
tests pin both halves: the engine revokes the right grants, and the events that end a session or
change the conditions a command was confirmed under actually reach it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
import yaml

from nox.core.config import NoxConfig
from nox.core.state import PrivacyMode
from nox.security.model import Risk, TemporaryGrant
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore
from nox.security.secrets import InMemorySecretStore
from nox.security.service import SecurityContext
from tests.unit.fakes import FakeBus

from .conftest import DEFAULTS_YAML, PROFILES_DIR, START, MutableClock

CMD_SCOPE = "cmd:/usr/bin/whoami#0123456789abcdef argv=" + "0" * 32


def _grant(engine: DefaultPermissionEngine, grant_id: str, scope: str) -> None:
    engine.grant_temporary(
        TemporaryGrant(
            grant_id=grant_id,
            scope=scope,
            max_risk=Risk.HIGH,
            expires_at=START + timedelta(hours=1),
        )
    )


def _scopes(grants: InMemoryGrantStore) -> set[str]:
    return {grant.scope for grant in grants.active(START)}


def _seed(engine: DefaultPermissionEngine) -> None:
    _grant(engine, "cmd", CMD_SCOPE)
    _grant(engine, "session", "session:obs/scene.switch")
    _grant(engine, "task", "task:t1")


def test_end_session_revokes_session_and_cmd_grants_only(
    engine: DefaultPermissionEngine, grants: InMemoryGrantStore
) -> None:
    _seed(engine)
    assert engine.end_session(reason="conversation_end") == 2
    # A task grant is not a session grant: it ends with its task, not with the conversation.
    assert _scopes(grants) == {"task:t1"}
    assert engine.end_session() == 0  # nothing left to end


@pytest.mark.parametrize("content", ["page text", None])
def test_taint_revokes_cmd_grants(
    engine: DefaultPermissionEngine, grants: InMemoryGrantStore, content: str | None
) -> None:
    _seed(engine)
    engine.mark_tainted("c1", content)
    assert _scopes(grants) == {"session:obs/scene.switch", "task:t1"}


def test_taint_also_revokes_a_cmd_grant_made_while_already_tainted(
    engine: DefaultPermissionEngine, grants: InMemoryGrantStore
) -> None:
    """Every arrival of untrusted content revokes them, not only the first (owner decision, C10):
    a command remembered since, in another conversation, must not stay usable while untrusted
    content keeps arriving."""
    engine.mark_tainted("c1", "first page")
    _grant(engine, "cmd", CMD_SCOPE)  # remembered by the user in conversation c2
    engine.mark_tainted("c1", "second page")
    assert CMD_SCOPE not in _scopes(grants)
    _grant(engine, "cmd", CMD_SCOPE)
    engine.mark_tainted("c1", None)  # a paired device's next message during a session
    assert CMD_SCOPE not in _scopes(grants)


def test_untrusted_content_outside_any_conversation_always_revokes(
    engine: DefaultPermissionEngine, grants: InMemoryGrantStore
) -> None:
    engine.mark_tainted("", "page text")
    _grant(engine, "cmd", CMD_SCOPE)
    engine.mark_tainted("", "more page text")  # nothing to tie it to: revoked every time
    assert CMD_SCOPE not in _scopes(grants)


def test_a_tainted_conversation_creates_no_remembered_grant(
    engine: DefaultPermissionEngine, grants: InMemoryGrantStore
) -> None:
    import asyncio

    from nox.hands.shell import argv_digest
    from nox.security.model import Decision, PermissionRequest

    target = f"{CMD_SCOPE.split(' argv=')[0]} argv={argv_digest('whoami', ())} whoami"

    def request(conversation: str) -> PermissionRequest:
        return PermissionRequest.model_validate(
            {
                "agent": "companion",
                "tool": "shell",
                "action": "run",
                "mode": "companion",
                "risk": Risk.HIGH,
                "target": target,
                "conversation": conversation,
            }
        )

    async def scenario() -> None:
        engine.mark_tainted("c1", "page text")
        grant_id = engine.request_confirmation(request("c1"))
        (pending,) = [p for p in engine.pending_requests() if p.request_id == grant_id]
        assert pending.remember_ttl_s == 0  # "remember" is not offered here
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.decision is Decision.ALLOW and result.grant_id is None
        # The same question in a clean conversation may still be remembered.
        grant_id = engine.request_confirmation(request("c2"))
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        assert (await engine.await_confirmation(grant_id)).grant_id is not None

    asyncio.run(scenario())


def test_revoke_command_grants_reports_what_it_removed(
    engine: DefaultPermissionEngine, grants: InMemoryGrantStore
) -> None:
    _seed(engine)
    assert engine.revoke_command_grants(reason="test") == 1
    assert engine.revoke_command_grants(reason="test") == 0
    assert _scopes(grants) == {"session:obs/scene.switch", "task:t1"}


@pytest.fixture
def context(
    build_security: Callable[..., SecurityContext], bus: FakeBus, clock: MutableClock
) -> tuple[SecurityContext, InMemoryGrantStore]:
    config = NoxConfig.model_validate(yaml.safe_load(DEFAULTS_YAML.read_text(encoding="utf-8")))
    grants = InMemoryGrantStore()
    ctx = build_security(
        config,
        profiles_dir=PROFILES_DIR,
        bus=bus,
        secret_store=InMemorySecretStore(),
        grants=grants,
        clock=clock,
    )
    _seed(ctx.engine)
    return ctx, grants


async def test_a_privacy_mode_change_revokes_cmd_grants(
    context: tuple[SecurityContext, InMemoryGrantStore],
) -> None:
    ctx, grants = context
    await ctx.privacy.set_mode(PrivacyMode.BALANCED)  # no change: nothing happens
    assert CMD_SCOPE in _scopes(grants)
    await ctx.privacy.set_mode(PrivacyMode.PRIVATE)
    assert _scopes(grants) == {"session:obs/scene.switch", "task:t1"}


async def test_the_kill_switch_revokes_cmd_grants(
    context: tuple[SecurityContext, InMemoryGrantStore],
) -> None:
    ctx, grants = context
    report = await ctx.killswitch.engage("user", "test")
    assert report.hooks.get("permissions.cmd_grants") == "ok"
    assert _scopes(grants) == {"session:obs/scene.switch", "task:t1"}


def _simple_shell(conversation: str) -> object:
    from nox.hands.shell import argv_digest
    from nox.security.model import PermissionRequest

    target = f"{CMD_SCOPE.split(' argv=')[0]} argv={argv_digest('whoami', ())} whoami"
    return PermissionRequest.model_validate(
        {
            "agent": "companion",
            "tool": "shell",
            "action": "run",
            "mode": "companion",
            "risk": Risk.HIGH,
            "target": target,
            "conversation": conversation,
        }
    )


@pytest.mark.parametrize("tainted_when", ["asked", "answered"])
def test_remember_needs_a_clean_conversation_when_asked_and_when_answered(
    engine: DefaultPermissionEngine, tainted_when: str
) -> None:
    """Tainted when asked, cleared before the answer ("new conversation"): no grant. Clean when
    asked, tainted before the answer: no grant either (fail closed both ways)."""
    import asyncio

    from nox.security.model import Decision

    async def scenario() -> None:
        if tainted_when == "asked":
            engine.mark_tainted("c1", "page text")
        grant_id = engine.request_confirmation(_simple_shell("c1"))  # type: ignore[arg-type]
        if tainted_when == "asked":
            engine.clear_taint("c1")
        else:
            engine.mark_tainted("c1", "page text")
        (pending,) = [p for p in engine.pending_requests() if p.request_id == grant_id]
        assert pending.remember_ttl_s == 0
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.decision is Decision.ALLOW and result.grant_id is None

    asyncio.run(scenario())
