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
    engine.mark_tainted("c1", "first page")
    _grant(engine, "cmd", CMD_SCOPE)
    engine.mark_tainted("c1", "second page")
    assert CMD_SCOPE not in _scopes(grants)


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
