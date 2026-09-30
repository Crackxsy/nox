"""The `desktop.*` tools: what is running, what is open, and the four things Nox may change.

A model does not know window handles, so every window tool takes a title fragment instead - and the
interesting case is not one match, it is several. "Close Chrome" with four Chrome windows open does
*not* pick one: it reports the candidates and changes nothing. A guess here closes the wrong window,
and the user cannot tell that a guess was made.

The pair `desktop.window_close` and `desktop.process_stop` is the other place where the wording is
the safety feature. Closing asks the program, which may still save or refuse; stopping kills it and
loses whatever was unsaved. The descriptions say that in those words, because a model picking the
wrong one of the two costs the user work.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, model_validator

from nox.core.logging import get_logger
from nox.desktop import processes
from nox.desktop.boundary import BoundaryError, check_process
from nox.desktop.win32 import WindowInfo, WindowProbe
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = ["register_desktop_tools"]

#: Entries one process listing returns. A machine has hundreds; 25 by memory answers every question
#: a person actually asks ("what is eating my RAM", "is Discord running").
PROCESS_LIMIT = 25

GamePids = Callable[[], set[int]]


class Empty(BaseModel):
    """No arguments."""


class WindowRef(BaseModel):
    """Which window: a piece of its title, or a handle from a previous listing."""

    title: str = Field(default="", max_length=200)
    handle: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _exactly_one(self) -> WindowRef:
        if bool(self.title.strip()) == bool(self.handle):
            raise ValueError("give either a title fragment or a handle, not both and not neither")
        return self


class ProcessRef(BaseModel):
    pid: int = Field(gt=0)


def _matches(windows: list[WindowInfo], ref: WindowRef) -> list[WindowInfo]:
    if ref.handle:
        return [window for window in windows if window.handle == ref.handle]
    needle = ref.title.strip().lower()
    return [
        window
        for window in windows
        if needle in window.title.lower() or needle in window.process_name.lower()
    ]


def _as_dict(window: WindowInfo) -> dict[str, Any]:
    return {
        "handle": window.handle,
        "title": window.title,
        "process": window.process_name,
        "pid": window.pid,
        "minimized": window.minimized,
    }


def _one(windows: list[WindowInfo], ref: WindowRef) -> WindowInfo | dict[str, Any]:
    """The single matching window, or the answer to give instead of guessing."""
    found = _matches(windows, ref)
    if not found:
        asked = ref.title or ref.handle
        return {"ok": False, "error": f"no open window matches {asked!r}"}
    if len(found) > 1:
        return {
            "ok": False,
            "error": f"{len(found)} windows match; say which one by its handle",
            "candidates": [_as_dict(window) for window in found],
        }
    return found[0]


def _build_windows(probe: WindowProbe, game_pids: GamePids) -> ToolSpec:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        games = game_pids()
        return {
            "ok": True,
            "windows": [
                # The game is marked, not hidden: knowing it is open is observation, which is
                # allowed. Acting on it is what the boundary refuses.
                {**_as_dict(window), "is_game": window.pid in games}
                for window in probe.windows()
            ],
        }

    return ToolSpec(
        name="desktop.windows",
        description="The open windows: title, program and whether each one is minimised.",
        input_model=Empty,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def _build_processes() -> ToolSpec:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return await processes.list_processes(limit=PROCESS_LIMIT)

    return ToolSpec(
        name="desktop.processes",
        description=(
            f"The {PROCESS_LIMIT} programs using the most memory, with their process ids. Use this "
            "to find out what is running before suggesting closing anything."
        ),
        input_model=Empty,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def _window_action(
    name: str,
    description: str,
    risk: Risk,
    act: Callable[[int], bool],
    probe: WindowProbe,
    game_pids: GamePids,
    *,
    verb: str,
) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        ref = WindowRef.model_validate(payload)
        window = _one(probe.windows(), ref)
        if isinstance(window, dict):
            return window
        try:
            check_process(window.pid, window.process_name, game_pids=game_pids())
        except BoundaryError as exc:
            log.info("desktop.window_refused", action=name, pid=window.pid, reason=str(exc))
            return {"ok": False, "error": str(exc)}
        worked = act(window.handle)
        return {
            "ok": worked,
            "window": _as_dict(window),
            "error": "" if worked else f"Windows did not {verb} that window",
        }

    return ToolSpec(
        name=name,
        description=description,
        input_model=WindowRef,
        risk=risk,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("title") or payload.get("handle") or ""),
    )


def _build_stop(game_pids: GamePids) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        return await processes.stop_process(int(payload["pid"]), game_pids=game_pids())

    return ToolSpec(
        name="desktop.process_stop",
        description=(
            "End a program by its process id. Anything unsaved in it is lost, so prefer "
            "desktop.window_close, which asks the program to close. Use this when asking failed."
        ),
        input_model=ProcessRef,
        risk=Risk.HIGH,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("pid") or ""),
    )


def register_desktop_tools(registry: ToolRegistry, probe: WindowProbe, game_pids: GamePids) -> None:
    """Register the six `desktop.*` tools."""
    actions = (
        (
            "desktop.window_focus",
            "Bring one window to the front. Says whether it really ended up there.",
            Risk.LOW,
            probe.focus,
            "bring that window to the front",
        ),
        ("desktop.window_minimize", "Minimise one window.", Risk.LOW, probe.minimize, "minimise"),
        (
            "desktop.window_restore",
            "Restore one minimised window without bringing it to the front.",
            Risk.LOW,
            probe.restore,
            "restore",
        ),
        (
            "desktop.window_close",
            (
                "Ask a program to close, the way its X button does - it may still ask to save. "
                "This does not kill it; a true answer means asked, not gone."
            ),
            Risk.MEDIUM,
            probe.close,
            "close",
        ),
    )
    specs = [_build_windows(probe, game_pids), _build_processes()]
    specs += [
        _window_action(name, description, risk, act, probe, game_pids, verb=verb)
        for name, description, risk, act, verb in actions
    ]
    specs.append(_build_stop(game_pids))
    for spec in specs:
        registry.register(spec)
    log.info("desktop.tools_registered", count=len(specs))
