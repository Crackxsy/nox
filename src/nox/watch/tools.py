"""`watch.create`, `watch.list`, `watch.delete`: "sag mir, wenn ..." as tools.

`watch.create` is `medium`: it does not change the world, but it makes Nox poll something for days
on the user's behalf, so the user sees exactly what will be watched before it starts. Listing is
`read`, deleting is `low` (it only ever makes Nox do less).

The most important sentence in the descriptions is the one about missing values. "Sag mir, wenn mein
Zug Verspätung hat" does not say which train; "unter 15 Euro" names a price, "wenn es günstig ist"
does not. A watcher built on a guessed station watches the wrong thing for weeks and nobody notices.
So the tool insists on knowing where each value came from, and the description tells the model to
ask - an answer the user gives is kept by the memory's fact capture like anything else they say.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from nox.core.config.hands import WatchConfig
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec
from nox.watch.condition import Op, parse_path
from nox.watch.watchers import (
    WatcherRepository,
    WatchError,
    WatcherRow,
    watchable_refusal,
)

__all__ = ["register_watch_tools", "watcher_dict"]


class WatchCreateInput(BaseModel):
    title: str = Field(
        min_length=3,
        max_length=200,
        description="What to tell the user when it happens, in their words "
        "(e.g. 'Dein Zug RB 40 hat Verspaetung').",
    )
    tool: str = Field(
        min_length=3,
        max_length=120,
        description="One read-only tool to call each time, e.g. web.fetch or calendar.upcoming.",
    )
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments for that tool.")
    path: str = Field(
        default="",
        max_length=200,
        description="Where in the tool's answer the value is: dotted keys and list indexes "
        "(e.g. 'structured.price' or 'content'). Empty = the whole answer.",
    )
    near: str = Field(
        default="",
        max_length=80,
        description="For text answers: only look at the rest of the line after this literal text "
        "(e.g. the train line 'RB 40' on a timetable page).",
    )
    op: Op = Field(description="lt / gt (numbers), contains (text or list), changed.")
    value: str | float | int | None = Field(
        default=None, description="The threshold or text to compare with; not used by 'changed'."
    )
    value_from: Literal["user", "memory", "none"] = Field(
        description="Where `value` and any personal detail in `args` (station, product, place) "
        "came from: the user's own words now, remembered facts, or none needed. Never invent one: "
        "if it is missing, ask the user first and do not call this tool."
    )
    interval_min: int = Field(default=15, ge=1, le=10080, description="Minutes between checks.")
    cooldown_min: int | None = Field(
        default=None, ge=0, le=10080, description="Minimum minutes between two reports."
    )

    @model_validator(mode="after")
    def _value_present(self) -> WatchCreateInput:
        if self.op is not Op.CHANGED and (self.value is None or str(self.value).strip() == ""):
            raise ValueError(f"op {self.op.value!r} needs a value; ask the user for it")
        if self.op is not Op.CHANGED and self.value_from == "none":
            raise ValueError("a threshold comes from the user or from memory, never from nowhere")
        parse_path(self.path)
        return self


class WatchDeleteInput(BaseModel):
    id: str = Field(min_length=1, max_length=40, description="The watcher's id, from watch.list.")


class _Empty(BaseModel):
    pass


def watcher_dict(row: WatcherRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "tool": row.tool,
        "args": row.args,
        "condition": {"path": row.path, "near": row.near, "op": row.op.value, "value": row.value},
        "interval_min": row.interval_min,
        "cooldown_min": row.cooldown_min,
        "created_at": row.created_at.isoformat(timespec="minutes"),
        "last_checked_at": row.last_checked_at.isoformat(timespec="minutes")
        if row.last_checked_at
        else None,
        "last_value": row.last_value,
        "last_fired_at": row.last_fired_at.isoformat(timespec="minutes")
        if row.last_fired_at
        else None,
        "last_error": row.last_error,
        "checks": row.checks,
        "fired": row.fired,
    }


def register_watch_tools(
    registry: ToolRegistry,
    repo: WatcherRepository,
    config: Callable[[], WatchConfig],
    clock: Callable[[], datetime],
) -> None:
    async def create(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = WatchCreateInput.model_validate(arguments)
        cfg = config()
        refusal = watchable_refusal(registry.get(payload.tool), payload.tool)
        if refusal is not None:
            return {"ok": False, "error": refusal}
        if repo.count_active() >= cfg.max_watchers:
            return {"ok": False, "error": f"there are already {cfg.max_watchers} watchers"}
        interval = max(payload.interval_min, cfg.min_interval_min)
        cooldown = (
            cfg.default_cooldown_min if payload.cooldown_min is None else payload.cooldown_min
        )
        try:
            row = repo.add(
                title=payload.title,
                tool=payload.tool,
                args=payload.args,
                path=payload.path,
                near=payload.near,
                op=payload.op,
                value=payload.value,
                interval_min=interval,
                cooldown_min=cooldown,
                source=f"tool:{payload.value_from}",
                now=clock(),
            )
        except (WatchError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        answer = {"ok": True, **watcher_dict(row)}
        if interval != payload.interval_min:
            answer["note"] = f"checks at most every {cfg.min_interval_min} minutes"
        return answer

    async def list_all(_arguments: dict[str, Any]) -> dict[str, Any]:
        return {"watchers": [watcher_dict(row) for row in repo.list_active()]}

    async def delete(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = WatchDeleteInput.model_validate(arguments)
        return {"ok": repo.delete(payload.id, clock())}

    registry.register(
        ToolSpec(
            name="watch.create",
            description=(
                "Tell the user when something happens ('sag mir, wenn ...'): one read-only tool "
                "checked every few minutes for a condition. Ask for missing details (which train, "
                "which price) - never guess."
            ),
            input_model=WatchCreateInput,
            risk=Risk.MEDIUM,
            handler=create,
            side_effects=True,
            local=True,
            targets=lambda payload: str(payload.get("tool") or ""),
        )
    )
    registry.register(
        ToolSpec(
            name="watch.list",
            description="Active watchers and what each last saw.",
            input_model=_Empty,
            risk=Risk.READ,
            handler=list_all,
            side_effects=False,
            local=True,
        )
    )
    registry.register(
        ToolSpec(
            name="watch.delete",
            description="Stop a watcher (id from watch.list).",
            input_model=WatchDeleteInput,
            risk=Risk.LOW,
            handler=delete,
            side_effects=True,
            local=True,
            targets=lambda payload: str(payload.get("id") or ""),
        )
    )
