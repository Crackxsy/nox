"""The `file.*` tools, and the risk level each one carries.

Risk is not a label here, it is what the permission engine decides on, so the levels are the design:

* `file.list` and `file.read` are READ. Nothing changes; the boundary already decided which folders
  exist at all, so looking inside them needs no dialog.
* `file.write`, `file.append` and `file.move` are MEDIUM, which the engine turns into a confirmation
  in any profile that has not explicitly allowed them. Reversible in principle, invisible in
  practice: nobody notices a changed file until they open it.
* `file.delete` is HIGH, and it deletes to the Recycle Bin or not at all.

Every tool reports its path as the permission `target`, which is what lets a profile rule narrow by
folder - "writing is fine under Downloads, ask me anywhere else" is then a rule, not a code change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from nox.core.config.files import FilesConfig
from nox.core.logging import get_logger
from nox.files import ops
from nox.files.roots import OutsideRootsError, resolved_roots
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = ["register_file_tools"]

Settings = Callable[[], FilesConfig]

PATH_FIELD = Field(min_length=1, max_length=400)


class PathInput(BaseModel):
    path: str = PATH_FIELD


class WriteInput(BaseModel):
    path: str = PATH_FIELD
    text: str = Field(max_length=1_000_000)
    #: Both default to false so the safe thing is what happens when the model says nothing.
    append: bool = False
    overwrite: bool = False


class MoveInput(BaseModel):
    path: str = PATH_FIELD
    to: str = PATH_FIELD
    overwrite: bool = False


class RootsInput(BaseModel):
    """`file.roots` takes no arguments."""


def _refusal(exc: OutsideRootsError) -> dict[str, Any]:
    """A boundary refusal, as an answer the model can pass on to the user."""
    log.info("files.refused", reason=str(exc))
    return {"ok": False, "error": str(exc)}


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _guarded(handler: Handler) -> Handler:
    """Turn the one exception the boundary raises into the answer shape every tool returns."""

    async def run(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await handler(payload)
        except OutsideRootsError as exc:
            return _refusal(exc)

    return run


def _build_roots(settings: Settings) -> ToolSpec:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        config = settings()
        present = [str(root) for root in resolved_roots(config.roots)]
        missing = [entry for entry in config.roots if str(entry) not in present]
        return {
            "ok": True,
            "folders": present,
            # A configured root that is not there (an unplugged drive) is not the same as one that
            # was never configured, and the difference is the user's to act on.
            "configured_but_missing": missing,
        }

    return ToolSpec(
        name="file.roots",
        description=(
            "The folders you may look at and change. Everything else is refused. Ask this before "
            "guessing a path."
        ),
        input_model=RootsInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def _build_list(settings: Settings) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        config = settings()
        return await ops.list_dir(str(payload["path"]), config.roots, limit=config.max_list_entries)

    return ToolSpec(
        name="file.list",
        description="What is in one folder: names, sizes and when each was last changed.",
        input_model=PathInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=_guarded(handler),
        targets=lambda payload: str(payload.get("path") or ""),
    )


def _build_read(settings: Settings) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        config = settings()
        return await ops.read_text(
            str(payload["path"]), config.roots, max_bytes=config.max_read_bytes
        )

    return ToolSpec(
        name="file.read",
        description="The text of one file. Refuses anything that is not text, and caps the size.",
        input_model=PathInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=_guarded(handler),
        targets=lambda payload: str(payload.get("path") or ""),
    )


def _build_write(settings: Settings) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        config = settings()
        return await ops.write_text(
            str(payload["path"]),
            str(payload["text"]),
            config.roots,
            max_bytes=config.max_write_bytes,
            append=bool(payload.get("append", False)),
            overwrite=bool(payload.get("overwrite", False)),
        )

    return ToolSpec(
        name="file.write",
        description=(
            "Write text to a file. Refuses to replace an existing file unless you pass "
            "overwrite, and never creates missing folders."
        ),
        input_model=WriteInput,
        risk=Risk.MEDIUM,
        side_effects=True,
        local=True,
        handler=_guarded(handler),
        targets=lambda payload: str(payload.get("path") or ""),
    )


def _build_move(settings: Settings) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        config = settings()
        return await ops.move(
            str(payload["path"]),
            str(payload["to"]),
            config.roots,
            overwrite=bool(payload.get("overwrite", False)),
        )

    return ToolSpec(
        name="file.move",
        description="Move or rename a file. Both ends have to be in a folder you configured.",
        input_model=MoveInput,
        risk=Risk.MEDIUM,
        side_effects=True,
        local=True,
        handler=_guarded(handler),
        targets=lambda payload: str(payload.get("path") or ""),
    )


def _build_delete(settings: Settings) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        return await ops.delete(str(payload["path"]), settings().roots)

    return ToolSpec(
        name="file.delete",
        description=(
            "Move a file to the Recycle Bin, where it can be got back. There is no delete that "
            "cannot be undone."
        ),
        input_model=PathInput,
        risk=Risk.HIGH,
        side_effects=True,
        local=True,
        handler=_guarded(handler),
        targets=lambda payload: str(payload.get("path") or ""),
    )


def register_file_tools(registry: ToolRegistry, settings: Settings) -> None:
    """Register the `file.*` tools. `file.delete` only when the Recycle Bin is the way out."""
    specs = [
        _build_roots(settings),
        _build_list(settings),
        _build_read(settings),
        _build_write(settings),
        _build_move(settings),
    ]
    if settings().delete_to_recycle_bin:
        specs.append(_build_delete(settings))
    for spec in specs:
        registry.register(spec)
    log.info("files.tools_registered", count=len(specs))
