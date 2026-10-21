"""Grants remembered for a node (`device:<id>|<scope>`) fall to every revocation path that
removes the same grant on the core - and to the node's own removal. One table over every grant
prefix and every path, so a new prefix or a new path cannot miss the device form."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from nox.security.model import Risk, TemporaryGrant
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore

LAPTOP = "a" * 32
PC = "b" * 32

#: Every grant scope kind there is, each in one example.
SCOPES = [
    "session:notify/show",
    "cmd:/usr/bin/df#0123456789abcdef argv=0123456789abcdef",
    "site:click@https://shop.example",
    "task:t1",
    "project:p1",
    "repo:/srv/repo",
    "session:mcp/weather.forecast",
    "session:skill/demo.run",
]


def _grant(scope: str, gid: str, clock: Any) -> TemporaryGrant:
    return TemporaryGrant(
        grant_id=gid,
        scope=scope,
        max_risk=Risk.MEDIUM,
        expires_at=clock() + timedelta(hours=1),
        granted_by="user",
    )


def _fill(engine: DefaultPermissionEngine, clock: Any) -> None:
    for i, scope in enumerate(SCOPES):
        engine.grant_temporary(_grant(scope, f"core{i}", clock))
        engine.grant_temporary(_grant(f"device:{LAPTOP}|{scope}", f"lap{i}", clock))
        engine.grant_temporary(_grant(f"device:{PC}|{scope}", f"pc{i}", clock))


def _left(grants: InMemoryGrantStore, clock: Any) -> set[str]:
    return {g.scope for g in grants.active(clock())}


def _revoked_by(path: str, engine: DefaultPermissionEngine) -> None:
    if path == "session_end":
        engine.end_session(reason="test")
    elif path in ("privacy_change", "kill_switch", "taint"):
        if path == "taint":
            engine.mark_tainted("conv-1", "untrusted text")
        else:
            engine.revoke_command_grants(reason=path)
    elif path == "mcp_removed":
        engine.revoke_scope("session:mcp/weather.", by="mcp")
    elif path == "skill_removed":
        engine.revoke_scope("session:skill/demo.", by="skills")
    else:  # pragma: no cover
        raise AssertionError(path)


#: path -> the scope prefixes it must remove.
PATHS = {
    "session_end": ("session:", "cmd:"),
    "privacy_change": ("cmd:",),
    "kill_switch": ("cmd:",),
    "taint": ("cmd:",),
    "mcp_removed": ("session:mcp/weather.",),
    "skill_removed": ("session:skill/demo.",),
}


@pytest.mark.parametrize("path", sorted(PATHS))
def test_every_revocation_path_covers_the_device_form(
    path: str, engine: DefaultPermissionEngine, grants: InMemoryGrantStore, clock: Any
) -> None:
    _fill(engine, clock)
    _revoked_by(path, engine)
    left = _left(grants, clock)
    for scope in SCOPES:
        gone = scope.startswith(PATHS[path])
        for form in (scope, f"device:{LAPTOP}|{scope}", f"device:{PC}|{scope}"):
            assert (form not in left) is gone, (path, form)


def test_removing_a_node_revokes_exactly_its_grants(
    engine: DefaultPermissionEngine, grants: InMemoryGrantStore, clock: Any
) -> None:
    _fill(engine, clock)
    assert engine.revoke_device_grants(LAPTOP, reason="node removed") == len(SCOPES)
    left = _left(grants, clock)
    assert not any(s.startswith(f"device:{LAPTOP}|") for s in left)
    assert {f"device:{PC}|{s}" for s in SCOPES} <= left
    assert set(SCOPES) <= left
