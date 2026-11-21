"""The two things the AI layer tells the user on its own: a plan limit ran out, and a turn got no
answer.

The router publishes `ai.provider_limited` once per limit; the orchestrator publishes
`ai.turn_failed` for a spoken turn that no model answered. Both become one notification through
`ProactiveService.notify`, so the existing rules decide whether it is *spoken*: not while muted or
inside a privacy zone, not in a quiet privacy mode or quiet hours, not in a mode that asks for no
interruptions (a game, focus). The visible toast is never held back. This module adds only what
those rules do not know: the sentences, saying a limit once, and not repeating a failure on every
turn.
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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        cooldown_s: float = FAILURE_NOTICE_COOLDOWN_S,
    ) -> None:
        self._service = service
        self._language = language
        self._clock = clock
        self._monotonic = monotonic
        self._cooldown_s = cooldown_s
        #: Per provider, the `until` of the limit already announced: the same limit once.
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
        if self._limits.get(limited.provider) == until:
            return
        self._limits[limited.provider] = until
        text = limit_notice(
            self._language(),
            until=until,
            now=self._clock(),
            estimated=limited.estimated,
            fallback=limited.fallback,
        )
        await self._service.notify(
            "urgent", "resources", text, context_key=f"ai.limit:{limited.provider}"
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
        await self._service.notify("urgent", "resources", text, context_key=f"ai.turn:{cause}")
