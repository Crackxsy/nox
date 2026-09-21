"""Composition-root glue for the proactive/attention layer and its two EPIC-08 dashboard requests.

`install(core)` is the single call the integrator adds to `app.py` (this package never imports or
edits `app.py` itself, per the task's shared-file rule) - one line after `self._register_handlers()`
in `NoxApp.start()`:

    from nox.proactive.install import install
    install(self)

`core` only needs to duck-type the attributes used below (`bus`, `state`, `config`, `speech_policy`,
`registry`, `tool_registry`, `db`, and optionally `speaker`) - exactly what `NoxApp` already
exposes after `_register_handlers()` runs (see `nox.app.NoxApp.start`).
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel

from nox.core.events import Event
from nox.ipc.dispatch import EmptyPayload
from nox.proactive.dashboard import (
    HealthHistoryRequest,
    make_config_effective_handler,
    make_health_history_handler,
)
from nox.proactive.service import ProactiveService
from nox.proactive.store import NotificationStore
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec
from nox.voice.base import TtsRequest


class NotificationDismissRequest(BaseModel):
    """`proactive.notification.dismiss {id}` (#28)."""

    id: str


class _Core(Protocol):
    """Duck-typed subset of `NoxApp` this module needs (see module docstring)."""

    bus: Any
    state: Any
    config: Any
    speech_policy: Any
    registry: Any
    tool_registry: ToolRegistry
    db: Any
    speaker: Any


class _TextSpeaker:
    """Adapts `nox.app.WorkerSpeaker` (which speaks a `TtsRequest`) to the plain-text
    `nox.proactive.service.Speaker` protocol, so this package stays decoupled from the voice
    contract's exact request shape."""

    def __init__(self, speaker: Any, *, language: str) -> None:
        self._speaker = speaker
        self._language = language

    async def say(self, text: str, *, language: str = "") -> None:
        await self._speaker.say(
            TtsRequest(
                utterance_id=uuid4().hex,
                text=text,
                language=language or self._language,
            )
        )


def install(core: _Core) -> ProactiveService:
    """Wire `ProactiveService` into `core` and register its dashboard requests/tool. Returns the
    service so callers (or tests) can also reach it via the return value, not only `core.proactive`.
    """

    async def publish_event(name: str, payload: dict[str, Any]) -> None:
        await core.bus.publish(Event(name=name, payload=payload))

    speaker = None
    if getattr(core, "speaker", None) is not None:
        configured = core.config.identity.speech_language
        # TtsRequest wants a concrete "de"/"en"; "auto" (language-of-the-message) has no meaning
        # for a proactive utterance with no preceding user message to detect it from.
        language = configured if configured in ("de", "en") else "de"
        speaker = _TextSpeaker(core.speaker, language=language)

    pcfg = core.config.proactive
    store = NotificationStore(limit=pcfg.notification_store_limit, db=core.db)
    service = ProactiveService(
        state=core.state,
        config=core.config,
        speech_policy=core.speech_policy,
        publish_event=publish_event,
        speaker=speaker,
        store=store,
    )
    core.proactive = service  # type: ignore[attr-defined]

    reg = core.registry.register
    reg(
        "health.history",
        HealthHistoryRequest,
        make_health_history_handler(core.db),
        roles=("shell", "dashboard"),
    )
    reg(
        "config.effective",
        EmptyPayload,
        make_config_effective_handler(core.config),
        roles=("shell", "dashboard"),
    )

    async def tool_handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        return service.status().model_dump(mode="json")

    core.tool_registry.register(
        ToolSpec(
            name="proactive.status.read",
            description=(
                "Read the current proactivity/attention status: focus mode, quiet hours, "
                "effective interruption ceiling and budget used this hour, and recent "
                "notifications."
            ),
            input_model=EmptyPayload,
            risk=Risk.READ,
            side_effects=False,
            local=True,
            handler=tool_handler,
        )
    )

    async def dismiss_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        dismissed = await service.dismiss(str(arguments.get("id", "")))
        return {"ok": dismissed}

    core.tool_registry.register(
        ToolSpec(
            name="proactive.notification.dismiss",
            description="Dismiss a notification by id so it stops showing as active/unread.",
            input_model=NotificationDismissRequest,
            risk=Risk.LOW,
            side_effects=True,
            local=True,
            handler=dismiss_handler,
        )
    )

    return service


__all__ = ["install"]
