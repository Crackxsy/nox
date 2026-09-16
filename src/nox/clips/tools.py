"""Core-side `clip.*` tools (ST-15-01/06, Spec v0.6 §9): registered directly on the shared
`ToolRegistry`, same pattern as `nox.tools.builtin.register_v01_tools` - not through a plugin
manifest, because they operate on the core's own `clips`/`clip_markers` tables and filesystem
roots. `clip.export`/`clip.trim` are `medium` risk so the normal permission pipeline (confirm) and
audit trail apply; `clip.trim` fails safely with a clear reason when no cutting backend is
available (ffmpeg is not currently installed, Spec v0.6 §11) rather than raising or faking a cut."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nox.clips.repository import ClipRepository
from nox.clips.trim import UNAVAILABLE_REASON, TrimBackend, TrimError
from nox.core.config import ClipsConfig
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)


class ClipListInput(BaseModel):
    status: str | None = None  # new | reviewed | exported | discarded; None = all
    limit: int = Field(default=50, ge=1, le=500)


class ClipTagInput(BaseModel):
    clip_id: str
    tags: list[str] | None = None  # None = leave unchanged
    notes: str | None = None


class ClipExportInput(BaseModel):
    clip_id: str


class ClipTrimInput(BaseModel):
    clip_id: str
    in_s: float = Field(ge=0.0)
    out_s: float = Field(gt=0.0)


def _clip_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": row.source,
        "trigger_kind": row.trigger_kind,
        "origin_event_id": row.origin_event_id,
        "session_id": row.session_id,
        "file_path": row.file_path,
        "duration_s": row.duration_s,
        "created_at": row.created_at.isoformat(),
        "thumbnail_path": row.thumbnail_path,
        "tags": row.tags,
        "status": row.status,
        "parent_clip_id": row.parent_clip_id,
        "checksum": row.checksum,
        "notes": row.notes,
    }


def make_clip_list_tool(repo: ClipRepository) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        status = arguments.get("status") or None
        limit = int(arguments.get("limit") or 50)
        rows = repo.list_clips(status=status, limit=limit)
        return {"clips": [_clip_dict(r) for r in rows]}

    return ToolSpec(
        name="clip.list",
        description="List clips, optionally filtered by status.",
        input_model=ClipListInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def make_clip_tag_tool(repo: ClipRepository) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        clip_id = str(arguments["clip_id"])
        tags = arguments.get("tags")
        notes = arguments.get("notes")
        ok = repo.update_tags(clip_id, tags=None if tags is None else list(tags), notes=notes)
        if not ok:
            raise KeyError(f"unknown clip id {clip_id!r}")
        row = repo.get(clip_id)
        assert row is not None
        return {"clip": _clip_dict(row)}

    return ToolSpec(
        name="clip.tag",
        description="Update a clip's tags/notes.",
        input_model=ClipTagInput,
        risk=Risk.LOW,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("clip_id") or ""),
    )


def _copy_to_export(src: Path, export_root: Path) -> Path | None:
    """Blocking; runs off the event loop via `asyncio.to_thread`. `None` when `src` is missing."""
    if not src.is_file():
        return None
    export_root.mkdir(parents=True, exist_ok=True)
    dest = export_root / src.name
    shutil.copy2(src, dest)  # copy only - the library file (and the recording) stay untouched
    return dest


def make_clip_export_tool(repo: ClipRepository, config: ClipsConfig, bus: EventBus) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        clip_id = str(arguments["clip_id"])
        row = repo.get(clip_id)
        if row is None:
            raise KeyError(f"unknown clip id {clip_id!r}")
        src = Path(row.file_path)
        dest = await asyncio.to_thread(_copy_to_export, src, config.export_root)
        if dest is None:
            return {"ok": False, "export_path": None, "reason": f"clip file missing: {src}"}
        repo.set_status(clip_id, "exported")
        await bus.publish(
            Event(
                name=E.CLIP_EXPORTED,
                payload={"clip_id": clip_id, "export_path": str(dest)},
            )
        )
        return {"ok": True, "export_path": str(dest), "reason": ""}

    return ToolSpec(
        name="clip.export",
        description="Copy a clip into the configured export folder. No network call is ever made.",
        input_model=ClipExportInput,
        risk=Risk.MEDIUM,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("clip_id") or ""),
    )


def make_clip_trim_tool(
    repo: ClipRepository, config: ClipsConfig, backend: TrimBackend
) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        clip_id = str(arguments["clip_id"])
        in_s = float(arguments["in_s"])
        out_s = float(arguments["out_s"])
        if out_s <= in_s:
            return {"ok": False, "clip_id": None, "reason": "out_s must be greater than in_s"}
        row = repo.get(clip_id)
        if row is None:
            raise KeyError(f"unknown clip id {clip_id!r}")
        if not backend.available():
            # Never raised: a clear, non-crashing result the dashboard can show as-is (ST-15-06 AC).
            return {"ok": False, "clip_id": None, "reason": UNAVAILABLE_REASON}
        src = Path(row.file_path)
        dest = config.library_root / f"{src.stem}_trim_{int(in_s)}-{int(out_s)}{src.suffix}"
        try:
            await backend.trim(src, dest, in_s=in_s, out_s=out_s)
        except TrimError as exc:
            return {"ok": False, "clip_id": None, "reason": str(exc)}
        new_row = repo.insert(
            source="marker_promoted" if row.source == "marker_promoted" else row.source,
            trigger_kind=row.trigger_kind,
            file_path=str(dest),
            origin_event_id=row.origin_event_id,
            session_id=row.session_id,
            duration_s=max(0.0, out_s - in_s),
            tags=[*row.tags, "trim"],
            parent_clip_id=row.id,
        )
        return {"ok": True, "clip_id": new_row.id, "reason": ""}

    return ToolSpec(
        name="clip.trim",
        description=(
            "Trim a clip to [in_s, out_s] and write a new file (never edits the source). "
            "Fails with a clear reason when no cutting backend (ffmpeg) is available."
        ),
        input_model=ClipTrimInput,
        risk=Risk.MEDIUM,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("clip_id") or ""),
    )


def register_clip_tools(
    registry: ToolRegistry,
    repo: ClipRepository,
    config: ClipsConfig,
    bus: EventBus,
    backend: TrimBackend,
) -> None:
    registry.register(make_clip_list_tool(repo))
    registry.register(make_clip_tag_tool(repo))
    registry.register(make_clip_export_tool(repo, config, bus))
    registry.register(make_clip_trim_tool(repo, config, backend))
