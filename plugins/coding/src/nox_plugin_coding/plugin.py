"""Coding assistant plugin (`coding` profile only, `plugins/coding/manifest.yaml`).

Wraps `SessionRunner` (`.session`, a resumable, tool-enabled Claude Code CLI session) as four tools
- `coding.session.start`, `coding.session.status.read`, `coding.session.stop`,
`coding.review.request` - and terminates every active session on `security.kill_switch` (never
leave a session running unattended). No `network.egress`: the Claude Code CLI authenticates and
talks to Anthropic itself; this plugin makes no HTTP calls of its own.

`coding.session.start`'s `target` (the workspace root) must resolve under one of the manifest's
configured `filesystem_roots` (kept in step with `config/profiles/coding.yaml` by hand) - an out-
of-root target fails in the tool handler as a `ValueError`, the same "schema validation failure,
not an ambiguous confirm" pattern `obs.scene.switch` uses for its allow-listed `target` (PM and
Coding).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nox.plugins.api import PluginApi

from .review import format_review
from .session import (
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_MAX_TURNS,
    DEFAULT_PERMISSION_MODE,
    MAX_REPAIR_ATTEMPTS,
    SessionOutcome,
    SessionRecord,
    SessionRunner,
    SessionStage,
    ToolUseEvent,
)

EV_STARTED = "coding.session_started"
EV_PROGRESS = "coding.session_progress"
EV_ENDED = "coding.session_ended"
EV_FAILED = "coding.session_failed"


# ---- tool input models ------------------------------------------------------------------------


class EmptyInput(BaseModel):
    """Tools that take no arguments."""


class SessionStartInput(BaseModel):
    target: str = Field(..., min_length=1, description="workspace root, absolute path")
    prompt: str = Field(..., min_length=1, max_length=20000)
    story_id: str = ""
    project_id: str = ""
    max_turns: int = Field(default=DEFAULT_MAX_TURNS, ge=1, le=200)


class SessionStatusReadInput(BaseModel):
    session_id: str = Field(..., min_length=1)


class SessionStopInput(BaseModel):
    session_id: str = Field(..., min_length=1)
    reason: str = "cancelled"


class ReviewRequestInput(BaseModel):
    session_id: str = Field(..., min_length=1)


def _under_any_root(target: str, roots: list[str]) -> bool:
    """Same allow-list pattern as `obs.scene.switch`'s `scene_set` check : a runtime
    config list, checked in the handler, an out-of-list target is a validation failure."""
    if not roots:
        return False
    try:
        resolved = Path(target).resolve()
    except OSError:
        return False
    for root in roots:
        try:
            root_resolved = Path(root).resolve()
        except OSError:
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
    return False


class CodingPlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        self.roots: list[str] = [str(r) for r in api.config.get("filesystem_roots", [])]
        self.runner = SessionRunner(
            command=str(api.config.get("command", "claude")),
            default_model=api.config.get("model", "sonnet") or None,
            permission_mode=str(api.config.get("permission_mode", DEFAULT_PERMISSION_MODE)),
            allowed_tools=tuple(api.config.get("allowed_tools", DEFAULT_ALLOWED_TOOLS)),
            max_repair_attempts=int(api.config.get("max_repair_attempts", MAX_REPAIR_ATTEMPTS)),
            default_max_turns=int(api.config.get("max_turns", DEFAULT_MAX_TURNS)),
        )
        self._unsub_kill_switch: Any = None

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        self._unsub_kill_switch = self.api.events.on("security.kill_switch", self._on_kill_switch)

    async def stop(self) -> None:
        if self._unsub_kill_switch is not None:
            self._unsub_kill_switch()
            self._unsub_kill_switch = None
        await self.runner.terminate_all(reason="plugin_stop")

    # -- security.kill_switch --------------------------------------------------------------------

    async def _on_kill_switch(self, _name: str, _payload: dict[str, Any]) -> None:
        """Terminate every active session's subprocess immediately; report each one as failed so
        nothing is left silently dangling. Must never raise into the event
        bus - a failing report here must not block the kill switch itself."""
        terminated = await self.runner.terminate_all(reason="kill_switch")
        for record in terminated:
            try:
                await self.api.events.emit(
                    EV_FAILED,
                    {
                        "session_id": record.session_id,
                        "reason": "security.kill_switch engaged",
                        "repair_attempts": record.repair_attempts,
                        "detail": "session terminated by the kill switch",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - kill switch reporting must never raise
                self.api.log.warning(
                    "coding.kill_switch_report_failed", session_id=record.session_id, error=str(exc)
                )

    # -- progress callback -------------------------------------------------------------------------

    async def _on_started(self, record: SessionRecord) -> None:
        await self.api.events.emit(
            EV_STARTED,
            {
                "session_id": record.session_id,
                "story_id": "",
                "project_id": "",
                "workspace": record.workspace,
            },
        )

    async def _on_progress(
        self, record: SessionRecord, stage: SessionStage, tool_event: ToolUseEvent | None
    ) -> None:
        payload: dict[str, Any] = {
            "session_id": record.session_id,
            "stage": stage.value,
            "detail": "",
            "tool_name": "",
            "files": [],
        }
        if tool_event is not None:
            payload["tool_name"] = tool_event.tool_name
            touched = tool_event.file_touched
            payload["files"] = [touched] if touched else []
            payload["detail"] = f"{tool_event.tool_name} {touched}".strip()
        await self.api.events.emit(EV_PROGRESS, payload)

    # -- tools -------------------------------------------------------------------------------------

    async def session_start(self, data: SessionStartInput) -> dict[str, Any]:
        if not _under_any_root(data.target, self.roots):
            raise ValueError(
                f"{data.target!r} is not under a configured coding workspace root ({self.roots!r})"
            )
        record = await self.runner.run_with_repair(
            workspace=data.target,
            prompt=data.prompt,
            max_turns=data.max_turns,
            on_started=self._on_started,
            on_progress=self._on_progress,
        )
        await self._report_outcome(record)
        return _snapshot(record)

    async def session_status_read(self, data: SessionStatusReadInput) -> dict[str, Any]:
        record = self.runner.get(data.session_id)
        if record is None:
            return {"session_id": data.session_id, "found": False}
        return {"found": True, **_snapshot(record)}

    async def session_stop(self, data: SessionStopInput) -> dict[str, Any]:
        record = await self.runner.cancel(data.session_id, reason=data.reason)
        if record is None:
            return {"session_id": data.session_id, "found": False}
        await self.api.events.emit(
            EV_ENDED,
            {
                "session_id": record.session_id,
                "outcome": record.outcome.value,
                "summary": data.reason,
                "repair_attempts": record.repair_attempts,
            },
        )
        return {"found": True, **_snapshot(record)}

    async def review_request(self, data: ReviewRequestInput) -> dict[str, Any]:
        record = self.runner.get(data.session_id)
        review = format_review(record)
        return {"session_id": data.session_id, "text": review.as_text(), **review.as_dict()}

    async def _report_outcome(self, record: SessionRecord) -> None:
        if record.outcome is SessionOutcome.ENDED:
            await self.api.events.emit(
                EV_ENDED,
                {
                    "session_id": record.session_id,
                    "outcome": record.outcome.value,
                    "summary": record.last_result_text[:500],
                    "repair_attempts": record.repair_attempts,
                },
            )
        elif record.outcome is SessionOutcome.FAILED:
            review = format_review(record)
            await self.api.events.emit(
                EV_FAILED,
                {
                    "session_id": record.session_id,
                    "reason": record.last_error,
                    "repair_attempts": record.repair_attempts,
                    "detail": review.as_text(),
                },
            )


def _snapshot(record: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "cli_session_id": record.cli_session_id,
        "workspace": record.workspace,
        "stage": record.stage.value,
        "outcome": record.outcome.value,
        "repair_attempts": record.repair_attempts,
        "files_touched": list(record.files_touched),
        "last_error": record.last_error,
    }


def create(api: PluginApi) -> CodingPlugin:
    plugin = CodingPlugin(api)
    api.tools.register(
        "coding.session.start",
        SessionStartInput,
        plugin.session_start,
        "medium",
        description="Start a resumable, tool-enabled Claude Code session scoped to a workspace.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "coding.session.status.read",
        SessionStatusReadInput,
        plugin.session_status_read,
        "read",
        description="Read the current status of a tracked coding session.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "coding.session.stop",
        SessionStopInput,
        plugin.session_stop,
        "low",
        description="Cancel a running coding session gracefully.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "coding.review.request",
        ReviewRequestInput,
        plugin.review_request,
        "medium",
        description="Short triage of a coding session's outcome: what, why, next.",
        side_effects=False,
        local=True,
    )
    return plugin
