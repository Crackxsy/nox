"""Every IPC role is explicitly configured in every security table.

A role that a table forgets falls through to whatever that table does by default, which is how a
new role silently gets more than intended. This test fails for a role missing anywhere, so adding
a role forces a decision in each place.
"""

from __future__ import annotations

from typing import get_args

import pytest

from nox.ipc.dispatch import ROLE_ALLOWLIST
from nox.ipc.protocol import Role
from nox.ipc.server import EVENT_POLICY, role_may_see
from nox.ipc.tokens import (
    BOUND_ROLES,
    DEVICE_ROLES,
    HUBLESS_ROLES,
    OVERLAY_ROLES,
    SESSION_ROLES,
    WORKER_ROLES,
)

ROLES = get_args(Role)


@pytest.mark.parametrize("role", ROLES)
def test_every_role_has_a_request_allow_list(role: str) -> None:
    assert role in ROLE_ALLOWLIST


@pytest.mark.parametrize("role", ROLES)
def test_every_role_has_an_event_policy(role: str) -> None:
    assert EVENT_POLICY.get(role) in {"filtered", "none"}


@pytest.mark.parametrize("role", ROLES)
def test_every_role_authenticates_in_exactly_one_way(role: str) -> None:
    classes = [SESSION_ROLES, WORKER_ROLES, BOUND_ROLES, HUBLESS_ROLES, DEVICE_ROLES, OVERLAY_ROLES]
    assert sum(role in c for c in classes) == 1


def test_no_table_names_a_role_the_protocol_does_not_know() -> None:
    known = set(ROLES)
    assert set(ROLE_ALLOWLIST) <= known
    assert set(EVENT_POLICY) <= known


@pytest.mark.parametrize("role", sorted(BOUND_ROLES))
def test_bound_roles_see_no_event_at_all(role: str) -> None:
    for name in ("system.started", "chat.message", "voice.transcript_ready", "memory.updated"):
        assert role_may_see(role, name) is False
    assert ROLE_ALLOWLIST[role] == ()


def test_a_node_never_reaches_the_hub_and_sees_no_event() -> None:
    """`nox node` talks to its own listener only; chat, transcripts and memory never reach it."""
    assert "node" in HUBLESS_ROLES
    assert EVENT_POLICY["node"] == "none"
    assert ROLE_ALLOWLIST["node"] == ()
    for name in ("system.started", "chat.message", "voice.transcript_ready", "memory.updated"):
        assert role_may_see("node", name) is False


def test_an_unknown_role_sees_nothing() -> None:
    assert role_may_see("intruder", "system.started") is False
