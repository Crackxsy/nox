"""`ProactiveService.notify(kind, priority, text)`: the one place every unsolicited-notification
caller in Nox goes through (mirrors `SpeechPolicy`'s "one gate" shape for). Routes to TTS (via the
orchestrator's `Speaker`), a pet expression nudge (via `NoxState.update`), a dashboard toast
(`proactive.notification` event) and the remote channel (same event - the telegram plugin,, decides
on its own allow-list whether to forward it; that decision is out of this package's scope). hard
floor: `UrgentCategory.SECURITY` / `DATA_LOSS` always produce a visible toast even when speech is
muted - "never mute" in the epic summary means the *visual* channel is never filtered, not that a
mute setting is silently overridden for audio (that would violate the "Klappe halten" F11 hard-
stop). Only audio is gated by `SpeechPolicy.may_speak("urgent")`, and that call denies only on mute
(see `nox.core.speech_policy`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from nox.core.config import NoxConfig
from nox.core.logging import get_logger
from nox.core.speech_policy import SpeechPolicy, in_quiet_hours
from nox.proactive.attention import InterruptionBudget, effective_ceiling, is_focus_mode
from nox.proactive.feedback import FeedbackWeights
from nox.proactive.models import (
    INTERRUPT_ELIGIBLE,
    AttentionDecision,
    Channel,
    NotificationRecord,
    NotifyKind,
    ProactiveStatus,
    UrgentCategory,
)
from nox.proactive.store import NotificationStore
from nox.proactive.timing import TimingLearner

log = get_logger(__name__)


class StateReader(Protocol):
    def get(self, path: str) -> object: ...

    async def update(self, path: str, value: object, *, reason: str = "") -> int: ...


class Speaker(Protocol):
    """The subset of `nox.core.orchestrator.Speaker`/`WorkerSpeaker` this service needs."""

    async def say(self, text: str, *, language: str = "de") -> None: ...


class ProactiveService:
    def __init__(
        self,
        *,
        state: StateReader,
        config: NoxConfig,
        speech_policy: SpeechPolicy,
        publish_event: Callable[[str, dict[str, object]], Awaitable[None]],
        speaker: Speaker | None = None,
        clock: Callable[[], datetime] | None = None,
        budget: InterruptionBudget | None = None,
        timing: TimingLearner | None = None,
        feedback: FeedbackWeights | None = None,
        store: NotificationStore | None = None,
    ) -> None:
        self._state = state
        self._config = config
        self._speech_policy = speech_policy
        self._publish_event = publish_event
        self._speaker = speaker
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._budget = budget or InterruptionBudget(clock=self._clock)
        self._timing = timing or TimingLearner()
        pcfg = config.proactive
        self._feedback = feedback or FeedbackWeights(
            min_weight=pcfg.feedback_min_weight,
            max_weight=pcfg.feedback_max_weight,
            step=pcfg.feedback_step,
        )
        # `store or ...` used to silently replace a freshly-constructed, still-empty `store` here:
        # `NotificationStore` has `__len__`, so a caller-supplied store with 0 records so far (e.g.
        # `nox.proactive.install.install`'s db-backed store, right after boot) was falsy and got
        # swapped for a brand-new in-memory one -.
        self._store = (
            store if store is not None else NotificationStore(limit=pcfg.notification_store_limit)
        )

    # ---- public API -----------------------------------------------------------------------

    @property
    def timing(self) -> TimingLearner:
        return self._timing

    @property
    def feedback(self) -> FeedbackWeights:
        return self._feedback

    @property
    def store(self) -> NotificationStore:
        return self._store

    async def dismiss(self, notification_id: str) -> bool:
        """`proactive.notification.dismiss`. Publishes `proactive.notification.dismissed`
        only on an actual state change (unknown id / already-dismissed id -> no event, matching
        `_gate_hint`'s "no event for a no-op" shape elsewhere in this service)."""
        dismissed = self._store.dismiss(notification_id)
        if dismissed:
            await self._publish_event("proactive.notification.dismissed", {"id": notification_id})
        return dismissed

    def status(self) -> ProactiveStatus:
        mode = str(self._state.get("assistant.mode"))
        quiet = in_quiet_hours(self._config.attention.quiet_hours, self._clock().time())
        return ProactiveStatus(
            enabled=self._config.proactive.enabled,
            focus_mode=is_focus_mode(self._config.attention, mode),
            quiet_hours=quiet,
            effective_ceiling=effective_ceiling(self._config.attention, mode),
            interruptions_used_this_hour=self._budget.used(),
            interruptions_budget=self._config.attention.interruptions_per_hour,
            recent=self._store.list_recent(20),
        )

    async def notify(
        self,
        kind: NotifyKind,
        priority: str,
        text: str,
        *,
        context_key: str = "",
    ) -> AttentionDecision:
        if not self._config.proactive.enabled:
            return await self._suppress(kind, priority, "proactive_disabled")
        if kind == "urgent":
            category = UrgentCategory(priority)
            if category in INTERRUPT_ELIGIBLE:
                return await self._notify_interrupt_eligible(category, text)
            return await self._notify_lesser_urgent(category, text, context_key)
        return await self._notify_hint(priority, text, context_key)

    # ---- URGENT: security / data-loss (may interrupt) ---------------------------

    async def _notify_interrupt_eligible(
        self, category: UrgentCategory, text: str
    ) -> AttentionDecision:
        speak_allowed, _reason = self._speech_policy.may_speak("urgent")
        spoken = False
        if speak_allowed and self._speaker is not None:
            # exemption: URGENT itself is not announced first - it IS the time-critical
            # callout the announce rule exists to make way for.
            await self._speaker.say(text)
            spoken = True
        channel: Channel = "speech+toast" if spoken else "toast"
        record = self._record(
            kind="urgent",
            priority=category.value,
            text=text,
            channel=channel,
            spoken=spoken,
            announced=False,
        )
        await self._touch_pet_expression(urgent=True)
        await self._publish_notification(record)
        # Never suppressed: the toast/expression/event above always fire regardless of mute.
        return AttentionDecision(allowed=True, reason="ok", channel=channel, announced=False)

    # ---- URGENT: resources / task_result (visible warning, not an interruption) -----------

    async def _notify_lesser_urgent(
        self, category: UrgentCategory, text: str, context_key: str
    ) -> AttentionDecision:
        _ceiling, allow_speech, reason = self._gate_hint(context_key)
        spoken = False
        announced = False
        if allow_speech and self._speaker is not None:
            announced = await self._announce()
            await self._speaker.say(text)
            spoken = True
            self._budget.record()
        channel: Channel = "speech+toast" if spoken else "toast"
        record = self._record(
            kind="urgent",
            priority=category.value,
            text=text,
            channel=channel,
            spoken=spoken,
            announced=announced,
            suppressed_reason="" if allow_speech else reason,
        )
        await self._touch_pet_expression(urgent=False)
        await self._publish_notification(record)
        # A "lesser URGENT" is, by definition, always at least a visible warning - never
        # fully suppressed, only its speech may be held back.
        return AttentionDecision(allowed=True, reason="ok", channel=channel, announced=announced)

    # ---- ordinary proactive hints ----------------------------------------------------------

    async def _notify_hint(self, priority: str, text: str, context_key: str) -> AttentionDecision:
        """`priority` is expected to be one of `nox.proactive.models.HintPriority`
        (`"low" | "normal" | "high"`); kept as `str` here so `notify` has one signature for both
        the URGENT-category and plain-hint-priority domains."""
        _ceiling, allow_speech, reason = self._gate_hint(context_key)
        if not allow_speech:
            return await self._suppress("proactive", priority, reason, context_key=context_key)
        if self._config.proactive.timing_learner_enabled:
            hour = self._clock().hour
            if not self._timing.prefers_now(hour):
                return await self._suppress(
                    "proactive", priority, "timing_learner", context_key=context_key
                )
        announced = False
        spoken = False
        if self._speaker is not None:
            announced = await self._announce()
            await self._speaker.say(text)
            spoken = True
            self._budget.record()
        channel: Channel = "speech+toast" if spoken else "toast"
        record = self._record(
            kind="proactive",
            priority=priority,
            text=text,
            channel=channel,
            spoken=spoken,
            announced=announced,
        )
        await self._touch_pet_expression(urgent=False)
        await self._publish_notification(record)
        return AttentionDecision(allowed=True, reason="ok", channel=channel, announced=announced)

    # ---- shared gating / plumbing -----------------------------------------------------------

    def _gate_hint(self, context_key: str) -> tuple[int, bool, str]:
        """Returns `(density_ceiling, allowed, reason)`. `density_ceiling` (0..5, per-mode
        dial) only decides focus-mode (0 = no proactive hints at all); the actual hourly
        interruption cap is the separate `attention.interruptions_per_hour` count ("interruption
        budget"), not the 0..5 dial - the two are different units."""
        mode = str(self._state.get("assistant.mode"))
        density = effective_ceiling(self._config.attention, mode)
        allowed, reason = self._speech_policy.may_speak("proactive")
        if not allowed:
            return density, False, reason
        if density <= 0:
            return density, False, "focus_mode"
        if not self._budget.allow(self._config.attention.interruptions_per_hour):
            return density, False, "interruption_budget"
        if context_key:
            weight = self._feedback.weight(context_key)
            # A weight below 1.0 probabilistically thins repeat hints for a noisy context, without
            # ever fully silencing it (weight is clamped >= feedback_min_weight, never 0).
            if weight < 1.0 and (hash((context_key, self._clock().minute)) % 100) / 100 >= weight:
                return density, False, "feedback_weight"
        return density, True, "ok"

    async def _announce(self) -> bool:
        if not self._config.proactive.announce_before_speaking or self._speaker is None:
            return False
        name = self._config.identity.name or "Nox"
        await self._speaker.say(f"{name}:")
        return True

    async def _touch_pet_expression(self, *, urgent: bool) -> None:
        expression = "scared" if urgent else "curious"
        try:
            await self._state.update("assistant.expression", expression, reason="proactive.notify")
        except Exception:  # noqa: BLE001 - a pet-expression nudge must never break a notification
            log.debug("proactive.pet_expression_failed", expression=expression)

    def _record(
        self,
        *,
        kind: NotifyKind,
        priority: str,
        text: str,
        channel: str,
        spoken: bool,
        announced: bool,
        suppressed_reason: str = "",
    ) -> NotificationRecord:
        record = NotificationRecord(
            id=uuid4().hex,
            kind=kind,
            priority=priority,
            text=text,
            channel=channel,
            spoken=spoken,
            announced=announced,
            suppressed_reason=suppressed_reason,
        )
        self._store.add(record)
        return record

    async def _publish_notification(self, record: NotificationRecord) -> None:
        await self._publish_event(
            "proactive.notification",
            {
                "kind": record.kind,
                "priority": record.priority,
                "text": record.text,
                "channel": record.channel,
                "spoken": record.spoken,
                "announced": record.announced,
            },
        )

    async def _suppress(
        self, kind: NotifyKind, priority: str, reason: str, *, context_key: str = ""
    ) -> AttentionDecision:
        self._record(
            kind=kind,
            priority=priority,
            text="",
            channel="suppressed",
            spoken=False,
            announced=False,
            suppressed_reason=reason,
        )
        await self._publish_event(
            "proactive.suppressed", {"kind": kind, "priority": priority, "reason": reason}
        )
        return AttentionDecision(allowed=False, reason=reason, channel="suppressed")
