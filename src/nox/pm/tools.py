"""PM tool catalogue (subset of PM and Coding): list/read tools are `read` risk (allow by default,
no
confirm); `pm.story.create`/`pm.story.update_status` write to the vault and are `medium` risk,
which the default permission table resolves to `confirm`. The full status-workflow engine
(transition matrix, blocked-dependency rejection) and hash-based write-back conflict refusal are
later PM stories - this slice validates the target status against a fixed vocabulary and always
writes through the vault (vault wins), see this story's report Open Points.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, Field

from nox.core.events import E, Event, EventBus
from nox.core.state import StateManager
from nox.pm.focus import FocusService
from nox.pm.index import PmIndex
from nox.pm.models import WorkItem
from nox.pm.vault_repo import PmVaultRepo
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

StoryStatus = Literal["todo", "in_progress", "review", "done", "blocked"]


class _EmptyInput(BaseModel):
    pass


class _EpicListInput(BaseModel):
    status: str = ""


class _StoryListInput(BaseModel):
    epic_id: str = ""
    status: str = ""


class _StoryGetInput(BaseModel):
    id: str


class _StoryCreateInput(BaseModel):
    id: str = Field(pattern=r"^ST-\d+-\d+$")
    epic_id: str = Field(pattern=r"^EPIC-\d+$")
    title: str
    priority: str = "P2"
    estimate: str = "M"
    requirements: list[str] = Field(default_factory=list)


class _StoryUpdateStatusInput(BaseModel):
    id: str
    status: StoryStatus


def _item_changed_payload(item: WorkItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "epic_id": item.epic_id,
        "project_id": item.project_id,
    }


async def _sync_active(state: StateManager | None, index: PmIndex, item: WorkItem) -> None:
    """`ProjectState` updates: a story touched through a PM tool becomes the active story (and,
    transitively, its epic/project) in `NoxState.project`."""
    if state is None or item.kind != "story":
        return
    await state.update("project.active_story", item.id, reason="pm.story")
    if not item.epic_id:
        return
    await state.update("project.active_epic", item.epic_id, reason="pm.story")
    epic = index.get(item.epic_id)
    if epic is not None and epic.project_id:
        await state.update("project.active_project", epic.project_id, reason="pm.story")


def make_project_list_tool(repo: PmVaultRepo) -> ToolSpec:
    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        items = await asyncio.to_thread(repo.read_projects)
        return {"items": [i.model_dump(mode="json") for i in items]}

    return ToolSpec(
        name="pm.project.list",
        description="List projects from the PM vault's projects list note.",
        input_model=_EmptyInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def make_epic_list_tool(index: PmIndex) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        status = str(arguments.get("status") or "")
        items = index.list_items(kind="epic", statuses=(status,) if status else None)
        return {"items": [i.model_dump(mode="json") for i in items]}

    return ToolSpec(
        name="pm.epic.list",
        description="List epics from the PM index, optionally filtered by status.",
        input_model=_EpicListInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
        targets=lambda p: str(p.get("status") or ""),
    )


def make_story_list_tool(index: PmIndex) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        status = str(arguments.get("status") or "")
        epic_id = str(arguments.get("epic_id") or "")
        items = index.list_items(kind="story", statuses=(status,) if status else None)
        if epic_id:
            items = [i for i in items if i.epic_id == epic_id]
        return {"items": [i.model_dump(mode="json") for i in items]}

    return ToolSpec(
        name="pm.story.list",
        description="List stories from the PM index, optionally filtered by epic and/or status.",
        input_model=_StoryListInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
        targets=lambda p: str(p.get("epic_id") or ""),
    )


def make_story_get_tool(index: PmIndex, state: StateManager | None) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        story_id = str(arguments["id"])
        item = index.get(story_id)
        if item is None:
            raise KeyError(f"unknown story: {story_id!r}")
        await _sync_active(state, index, item)
        return {"item": item.model_dump(mode="json")}

    return ToolSpec(
        name="pm.story.get",
        description="Get one story by id from the PM index.",
        input_model=_StoryGetInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
        targets=lambda p: str(p.get("id") or ""),
    )


def make_story_create_tool(
    repo: PmVaultRepo, index: PmIndex, bus: EventBus, state: StateManager | None
) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        item = await asyncio.to_thread(
            repo.create_story,
            story_id=str(arguments["id"]),
            epic_id=str(arguments["epic_id"]),
            title=str(arguments["title"]),
            priority=str(arguments.get("priority") or "P2"),
            estimate=str(arguments.get("estimate") or "M"),
            requirements=list(arguments.get("requirements") or []),
        )
        await asyncio.to_thread(index.upsert, item)
        await bus.publish(Event(name=E.PM_ITEM_CHANGED, payload=_item_changed_payload(item)))
        await _sync_active(state, index, item)
        return {"item": item.model_dump(mode="json")}

    return ToolSpec(
        name="pm.story.create",
        description="Create a new story note from the Story Template under a confirmed epic.",
        input_model=_StoryCreateInput,
        risk=Risk.MEDIUM,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda p: str(p.get("id") or ""),
    )


def make_story_update_status_tool(
    repo: PmVaultRepo, index: PmIndex, bus: EventBus, state: StateManager | None
) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        item = await asyncio.to_thread(
            repo.update_story_status, str(arguments["id"]), str(arguments["status"])
        )
        await asyncio.to_thread(index.upsert, item)
        await bus.publish(Event(name=E.PM_ITEM_CHANGED, payload=_item_changed_payload(item)))
        await _sync_active(state, index, item)
        return {"item": item.model_dump(mode="json")}

    return ToolSpec(
        name="pm.story.update_status",
        description="Update a story's status frontmatter field and reindex it.",
        input_model=_StoryUpdateStatusInput,
        risk=Risk.MEDIUM,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda p: str(p.get("id") or ""),
    )


def make_focus_today_tool(focus: FocusService) -> ToolSpec:
    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        entries = focus.today()
        return {"items": [e.model_dump(mode="json") for e in entries]}

    return ToolSpec(
        name="pm.focus.today",
        description="Today's focus: open stories ranked by status/priority, with reasons.",
        input_model=_EmptyInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def register_pm_tools(
    registry: ToolRegistry,
    *,
    repo: PmVaultRepo,
    index: PmIndex,
    focus: FocusService,
    bus: EventBus,
    state: StateManager | None = None,
) -> None:
    registry.register(make_project_list_tool(repo))
    registry.register(make_epic_list_tool(index))
    registry.register(make_story_list_tool(index))
    registry.register(make_story_get_tool(index, state))
    registry.register(make_story_create_tool(repo, index, bus, state))
    registry.register(make_story_update_status_tool(repo, index, bus, state))
    registry.register(make_focus_today_tool(focus))
