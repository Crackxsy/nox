"""`computer.*` tools: a confirmed session, then pointer and key actions inside it.

`computer.session_start` is `high` and is never remembered across sessions: it asks every time.
When the user allows it, a temporary grant for `computer.pointer` and `computer.keys` (risk
`medium`) is issued for exactly the session's length, so the actions inside it do not ask again,
and it is revoked the moment the session ends - by time, by its action budget, by the stop hotkey,
by the kill switch or by `computer.session_end`. Every action is refused outright when no session
is open, whatever the permission engine said: the session is checked in `nox.desktop.computer_use`
itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, model_validator

from nox.core.config.hands import ComputerUseConfig
from nox.desktop.computer_use import ComputerUse, ComputerUseError, PointerAction
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

__all__ = ["register_computer_tools"]


class SessionStartInput(BaseModel):
    purpose: str = Field(
        min_length=5,
        max_length=200,
        description="What you will do with mouse and keyboard, in the user's words.",
    )
    minutes: int | None = Field(default=None, ge=1, le=120)


class PointerInput(BaseModel):
    action: PointerAction = Field(description="move, click, double_click, right_click or scroll.")
    x: int | None = Field(default=None, description="Screen x in pixels (omit for 'here').")
    y: int | None = Field(default=None, description="Screen y in pixels (omit for 'here').")
    amount: int = Field(default=0, ge=-10, le=10, description="Scroll notches, + up / - down.")

    @model_validator(mode="after")
    def _both_or_neither(self) -> PointerInput:
        if (self.x is None) != (self.y is None):
            raise ValueError("give both x and y, or neither")
        return self


class KeysInput(BaseModel):
    combo: str = Field(
        min_length=1, max_length=40, description="A key or combination, e.g. 'ctrl+s', 'alt+tab'."
    )


class _Empty(BaseModel):
    pass


Grant = Callable[[int], str]
Revoke = Callable[[str], None]
#: Drops a "remember" the user may have ticked on `computer.session_start`: it asks every time.
Unremember = Callable[[], None]


def register_computer_tools(
    registry: ToolRegistry,
    computer: ComputerUse,
    config: Callable[[], ComputerUseConfig],
    *,
    grant: Grant,
    revoke: Revoke,
    unremember: Unremember = lambda: None,
) -> Callable[[str], None]:
    """Register the four tools; returns `end(reason)` for the kill switch and the settings."""
    state: dict[str, str] = {}

    def end(reason: str) -> None:
        computer.end_session(reason)
        grant_id = state.pop("grant", None)
        if grant_id:
            revoke(grant_id)

    def checked(result: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return result()
        except ComputerUseError as exc:
            if not computer.active:
                end(str(exc))
            return {"ok": False, "error": str(exc)}

    async def start(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = SessionStartInput.model_validate(arguments)
        unremember()
        cfg = config()
        minutes = min(payload.minutes or cfg.session_minutes, cfg.session_minutes)

        def run() -> dict[str, Any]:
            end("a new session replaces it")
            computer.start_session(minutes=minutes, max_actions=cfg.max_actions_per_session)
            state["grant"] = grant(minutes)
            return {
                "ok": True,
                "minutes": minutes,
                "max_actions": cfg.max_actions_per_session,
                "stop": f"{cfg.stop_hotkey} stops it at once",
            }

        return checked(run)

    async def pointer(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = PointerInput.model_validate(arguments)
        return checked(
            lambda: {
                "ok": True,
                "done": computer.pointer(payload.action, payload.x, payload.y, payload.amount),
            }
        )

    async def keys(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = KeysInput.model_validate(arguments)
        return checked(lambda: {"ok": True, "pressed": computer.keys(payload.combo)})

    async def stop(_arguments: dict[str, Any]) -> dict[str, Any]:
        was_open = computer.active
        end("ended by Nox")
        return {"ok": True, "was_open": was_open}

    registry.register(
        ToolSpec(
            name="computer.session_start",
            description=(
                "Ask the user for a short session in which you may move and click the mouse and "
                "press key combinations. Off unless the user enabled computer use; never while a "
                "game runs. Prefer every other tool first."
            ),
            input_model=SessionStartInput,
            risk=Risk.HIGH,
            handler=start,
            side_effects=True,
            local=True,
            targets=lambda payload: str(payload.get("purpose") or "")[:200],
        )
    )
    registry.register(
        ToolSpec(
            name="computer.pointer",
            description="Inside a session: move, click, double-click, right-click or scroll.",
            input_model=PointerInput,
            risk=Risk.MEDIUM,
            handler=pointer,
            side_effects=True,
            local=True,
            targets=lambda payload: str(payload.get("action") or ""),
        )
    )
    registry.register(
        ToolSpec(
            name="computer.keys",
            description="Inside a session: press one key or combination such as ctrl+s.",
            input_model=KeysInput,
            risk=Risk.MEDIUM,
            handler=keys,
            side_effects=True,
            local=True,
            targets=lambda payload: str(payload.get("combo") or ""),
        )
    )
    registry.register(
        ToolSpec(
            name="computer.session_end",
            description="End the computer-use session now.",
            input_model=_Empty,
            risk=Risk.LOW,
            handler=stop,
            side_effects=True,
            local=True,
        )
    )
    return end
