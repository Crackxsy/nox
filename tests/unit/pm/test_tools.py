"""`nox.pm.tools`: permission paths (read=allow, write=medium->confirm) through a real
`ToolExecutor`/`DefaultPermissionEngine` pair (a "fake engine" setup, mirroring
`tests/unit/tools/test_executor.py`), plus `ProjectState` sync on story get/create/update_status.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from nox.core.events import E
from nox.core.state import PrivacyMode
from nox.pm.focus import FocusService
from nox.pm.index import PmIndex
from nox.pm.tools import register_pm_tools
from nox.pm.vault_repo import PmVaultRepo
from nox.security.audit import SqliteAuditLog
from nox.security.model import Decision, Profile
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore, PrivacySnapshot
from nox.security.profiles import InMemoryProfileProvider
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry
from tests.unit.fakes import FakeBus, FakeState


class _NoOpKillSwitch:
    def is_engaged(self) -> bool:
        return False


class _Privacy:
    """Mutable `PrivacySnapshotProvider` fixed at BALANCED (mirrors
    `tests/unit/tools/conftest.py::FakePrivacy`, kept local to avoid a cross-directory import)."""

    def snapshot(self) -> PrivacySnapshot:
        return PrivacySnapshot(
            mode=PrivacyMode.BALANCED, zone_active=False, safe_mode=False, panic=False
        )


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def state() -> FakeState:
    return FakeState()


@pytest.fixture
def audit(bus: FakeBus) -> SqliteAuditLog:
    return SqliteAuditLog(sqlite3.connect(":memory:"), bus=bus)


@pytest.fixture
def engine(audit: SqliteAuditLog, bus: FakeBus) -> DefaultPermissionEngine:
    profile = Profile(
        id="test", description="", rules=[], cloud_allowed=True, memory_writes_allowed=True
    )
    return DefaultPermissionEngine(
        profiles=InMemoryProfileProvider([profile]),
        privacy=_Privacy(),
        grants=InMemoryGrantStore(),
        audit=audit,
        bus=bus,
        initial_profile="test",
        confirm_timeout_s=0.2,
    )


@pytest.fixture
def executor(
    bus: FakeBus,
    repo: PmVaultRepo,
    index: PmIndex,
    state: FakeState,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
) -> ToolExecutor:
    index.rebuild(repo.all_items())
    focus = FocusService(index, bus, limit=5)
    registry = ToolRegistry()
    register_pm_tools(registry, repo=repo, index=index, focus=focus, bus=bus, state=state)
    return ToolExecutor(registry, engine, audit, bus, _NoOpKillSwitch())


# ---- read tools: allow, no confirm -------------------------------------------------------------


async def test_story_list_is_allowed_without_confirm(executor: ToolExecutor) -> None:
    result = await executor.call("nox.chat", "pm.story.list", {}, mode="companion")
    assert result.ok is True
    assert result.decision == Decision.ALLOW.value
    ids = {item["id"] for item in result.data["items"]}
    assert ids == {"ST-13-01", "ST-13-02"}


async def test_focus_today_is_read_and_ranked(executor: ToolExecutor) -> None:
    result = await executor.call("nox.chat", "pm.focus.today", {}, mode="companion")
    assert result.ok is True
    assert [i["id"] for i in result.data["items"]][0] == "ST-13-02"  # in_progress first


async def test_story_get_syncs_project_state(executor: ToolExecutor, state: FakeState) -> None:
    result = await executor.call("nox.chat", "pm.story.get", {"id": "ST-13-01"}, mode="companion")
    assert result.ok is True
    assert state.get("project.active_story") == "ST-13-01"
    assert state.get("project.active_epic") == "EPIC-13"


async def test_story_get_unknown_id_fails_cleanly(executor: ToolExecutor) -> None:
    result = await executor.call("nox.chat", "pm.story.get", {"id": "ST-99-99"}, mode="companion")
    assert result.ok is False


# ---- write tools: medium risk -> confirm required ------------------------------------------------


async def test_update_status_is_medium_risk_and_requires_confirm(
    executor: ToolExecutor, bus: FakeBus
) -> None:
    """No profile rule grants an explicit allow, so the executor's default-by-risk table
    (`Risk.MEDIUM -> Decision.CONFIRM`) applies; with nobody answering the confirmation prompt, the
    60s-capped wait times out and denies (confirm_timeout_s=0.2 here keeps the test fast)."""
    result = await executor.call(
        "nox.chat",
        "pm.story.update_status",
        {"id": "ST-13-01", "status": "in_progress"},
        mode="companion",
    )
    assert result.ok is False
    assert result.decision == Decision.DENY.value
    assert any(e.name == E.SECURITY_PERMISSION_REQUESTED for e in bus.published)


async def test_create_story_is_medium_risk_and_requires_confirm(executor: ToolExecutor) -> None:
    result = await executor.call(
        "nox.chat",
        "pm.story.create",
        {"id": "ST-13-20", "epic_id": "EPIC-13", "title": "Needs Confirm"},
        mode="companion",
    )
    assert result.ok is False
    assert result.decision == Decision.DENY.value


async def test_update_status_confirmed_writes_and_syncs_state(
    executor: ToolExecutor,
    bus: FakeBus,
    state: FakeState,
    index: PmIndex,
    engine: DefaultPermissionEngine,
) -> None:
    """Same confirm path as above, but this time something answers the confirmation grant
    (mirrors the dashboard/voice confirm flow, `tests/unit/tools/test_executor.py::
    test_confirm_then_allow`) - the write goes through, the index updates and `ProjectState`
    syncs."""

    async def _confirm_when_requested() -> None:
        event = await bus.wait_for(E.SECURITY_PERMISSION_REQUESTED)
        engine.reply(event.payload["request_id"], Decision.ALLOW)

    result, _ = await asyncio.gather(
        executor.call(
            "nox.chat",
            "pm.story.update_status",
            {"id": "ST-13-01", "status": "in_progress"},
            mode="companion",
        ),
        _confirm_when_requested(),
    )
    assert result.ok is True
    assert result.data["item"]["status"] == "in_progress"
    assert index.get("ST-13-01").status == "in_progress"  # type: ignore[union-attr]
    assert state.get("project.active_story") == "ST-13-01"
    assert any(e.name == E.PM_ITEM_CHANGED for e in bus.published)
