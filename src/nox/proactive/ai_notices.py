"""The two things the AI layer tells the user on its own: a plan limit ran out, and a turn got no
answer.

The router publishes `ai.provider_limited` once per limit; the orchestrator publishes
`ai.turn_failed` for a spoken turn that no model answered. Both become one notification through
`ProactiveService.notify`, which shows it as a toast and decides whether it may also be heard: not
while muted or inside a privacy zone, not in a quiet privacy mode or quiet hours, not in a mode
that asks for no interruptions (focus), and - added here - not while a game runs. A failed turn is
never spoken from here: the orchestrator already said it as the answer to what the user said, so
this only shows it, and not again for the same cause within a few minutes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError

from nox.ai.errors import NoProviderCause
from nox.ai.notices import failure_notice, limit_notice
from nox.core.events import E, Event, EventBus
from nox.core.events.system import AiProviderLimited, AiTurnFailed
from nox.core.logging import get_logger
from nox.core.speech_policy import SILENT_MODES
from nox.proactive.service import ProactiveService

log = get_logger(__name__)

__all__ = ["FAILURE_NOTICE_COOLDOWN_S", "AiNoticeRelay"]

#: The same failure is announced at most this often. A user who keeps talking to a model that is
#: down hears it once, not after every sentence; the chat reply of each typed turn is unaffected.
FAILURE_NOTICE_COOLDOWN_S = 300.0


class AiNoticeRelay:
    """Subscribes to the AI layer's events and hands their sentences to the proactive service."""

    def __init__(
        self,
        service: ProactiveService,
        *,
        language: Callable[[], str],
        mode: Callable[[], str] = lambda: "",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        cooldown_s: float = FAILURE_NOTICE_COOLDOWN_S,
    ) -> None:
        self._service = service
        self._language = language
        self._mode = mode
        self._clock = clock
        self._monotonic = monotonic
        self._cooldown_s = cooldown_s
        #: Per provider, when the limit announced last ends: one limit is announced once, however
        #: often its end is moved (a limit without a reset time is renewed every few minutes).
        self._limits: dict[str, datetime] = {}
        #: Per cause, when a failure was last announced.
        self._failed_at: dict[str, float] = {}
        self._unsubs: list[Callable[[], None]] = []

    def attach(self, bus: EventBus) -> None:
        self._unsubs = [
            bus.subscribe(E.AI_PROVIDER_LIMITED, self._on_limited),
            bus.subscribe(E.AI_TURN_FAILED, self._on_failed),
        ]

    def detach(self) -> None:
        for unsubscribe in self._unsubs:
            unsubscribe()
        self._unsubs.clear()

    async def _on_limited(self, event: Event) -> None:
        try:
            limited = AiProviderLimited.model_validate(event.payload)
            until = datetime.fromisoformat(limited.until)
        except (ValidationError, ValueError):
            log.warning("ai_notices.bad_payload", name=event.name)
            return
        now = self._clock()
        announced = self._limits.get(limited.provider)
        self._limits[limited.provider] = until
        if announced is not None and announced > now:
            return
        text = limit_notice(
            self._language(),
            until=until,
            now=now,
            estimated=limited.estimated,
            fallback=limited.fallback,
        )
        await self._service.notify(
            "urgent",
            "resources",
            text,
            context_key=f"ai.limit:{limited.provider}",
            speak=self._mode() not in SILENT_MODES,
        )

    async def _on_failed(self, event: Event) -> None:
        try:
            failed = AiTurnFailed.model_validate(event.payload)
            cause = NoProviderCause(failed.cause)
            until = datetime.fromisoformat(failed.until) if failed.until else None
        except (ValidationError, ValueError):
            log.warning("ai_notices.bad_payload", name=event.name)
            return
        now = self._clock()
        if cause is NoProviderCause.LIMIT and any(end > now for end in self._limits.values()):
            return  # the limit itself was announced; saying it again per turn is the repetition
        last = self._failed_at.get(cause.value)
        if last is not None and self._monotonic() - last < self._cooldown_s:
            return
        self._failed_at[cause.value] = self._monotonic()
        text = failure_notice(
            failed.language, cause, until=until, now=now, estimated=failed.estimated
        )
        await self._service.notify(
            "urgent", "resources", text, context_key=f"ai.turn:{cause}", speak=False
        )
