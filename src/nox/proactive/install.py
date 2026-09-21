"""Composition root for the proactive/attention layer and its two dashboard requests.

`install(core)` registers the `health.history` and `config.effective` requests, the two
`proactive.*` tools, and returns the runtime holding the service. `core` needs the attributes on
`_Core` below; they all exist once the core has finished registering its own handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    """Payload of `proactive.notification.dismiss`."""

    id: str


class _Core(Protocol):
    """The subset of the core this module needs."""

    bus: Any
    state: Any
    config: Any
    speech_policy: Any
    registry: Any
    tool_registry: ToolRegistry
    db: Any
    speaker: Any


@dataclass(slots=True)
class ProactiveRuntime:
    """Handle for the caller. Nothing here runs in the background: the service reacts to calls
    and the registered requests live on the core's registry, so `stop` has nothing to undo and
    exists so every extension is shut down the same way."""

    service: ProactiveService

    def stop(self) -> None:
        return None


class _TextSpeaker:
    """Adapts the core's speaker (which speaks a `TtsRequest`) to the plain-text `Speaker`
    protocol this package uses, so `nox.proactive` stays decoupled from the voice request shape."""

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


def install(core: _Core) -> ProactiveRuntime:
    """Wire the proactive service into `core`, register its requests and tools, and return it."""
    service = _build_service(core)
    _register_requests(core)
    _register_tools(core, service)
    return ProactiveRuntime(service=service)


def _build_service(core: _Core) -> ProactiveService:
    async def publish_event(name: str, payload: dict[str, Any]) -> None:
        await core.bus.publish(Event(name=name, payload=payload))

    speaker = None
    if core.speaker is not None:
        configured = core.config.identity.speech_language
        # TtsRequest wants a concrete "de"/"en"; "auto" (language-of-the-message) has no meaning
        # for a proactive utterance with no preceding user message to detect it from.
        language = configured if configured in ("de", "en") else "de"
        speaker = _TextSpeaker(core.speaker, language=language)

    pcfg = core.config.proactive
    return ProactiveService(
        state=core.state,
        config=core.config,
        speech_policy=core.speech_policy,
        publish_event=publish_event,
        speaker=speaker,
        store=NotificationStore(limit=pcfg.notification_store_limit, db=core.db),
    )


def _register_requests(core: _Core) -> None:
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


def _register_tools(core: _Core, service: ProactiveService) -> None:
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


__all__ = ["ProactiveRuntime", "install"]
