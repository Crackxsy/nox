"""Data shapes for the proactive/attention layer.

Kept dependency-light on purpose: this module has no import on `nox.app` or the IPC layer, so it
can be unit-tested in isolation (the project standards "each module gets tests").
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

#: Mirrors `nox.core.speech_policy.SpeechKind`'s proactive-relevant subset; kept separate so this
#: package never has to import from `nox.core.speech_policy` for typing alone.
NotifyKind = Literal["urgent", "proactive"]

#: Plain hint priority for `kind="proactive"` (not the URGENT category ordering below).
HintPriority = Literal["low", "normal", "high"]


class UrgentCategory(StrEnum):
    """/ F410 escalation ordering (Personality Specification v1): security/system beats
    backup/data-loss beats resources beats expected task results. Only `SECURITY` and `DATA_LOSS`
    may ever call `SpeechPolicy.may_speak("urgent")` (the mid-match/interrupt bypass) - `RESOURCES`
    and `TASK_RESULT` are "lesser URGENT": a visible warning, never a forced interruption."""

    SECURITY = "security"
    DATA_LOSS = "data_loss"
    RESOURCES = "resources"
    TASK_RESULT = "task_result"


#: ordering, most urgent first - used to sort a batch of pending urgent items for display.
URGENT_ORDER: tuple[UrgentCategory, ...] = (
    UrgentCategory.SECURITY,
    UrgentCategory.DATA_LOSS,
    UrgentCategory.RESOURCES,
    UrgentCategory.TASK_RESULT,
)

#: Only these categories may bypass zone/privacy-mode/quiet-hours via `may_speak("urgent")`.
INTERRUPT_ELIGIBLE: frozenset[UrgentCategory] = frozenset(
    {UrgentCategory.SECURITY, UrgentCategory.DATA_LOSS}
)


def urgent_sort_key(category: UrgentCategory) -> int:
    """Lower = more urgent; use as `sorted(items, key=lambda i: urgent_sort_key(i.category))`."""
    return URGENT_ORDER.index(category)


#: Where a `notify()` call actually went (or `"suppressed"` when it went nowhere).
Channel = Literal["speech", "toast", "speech+toast", "suppressed"]


class AttentionDecision(BaseModel):
    """What `nox.proactive.notify` decided for one call."""

    allowed: bool
    reason: str = "ok"  # "ok" when allowed, else a `proactive.suppressed` reason
    channel: Channel = "suppressed"
    announced: bool = False


class NotificationRecord(BaseModel):
    """One `proactive.notify` call, kept for `proactive.status.read` (notification
    store). Persisted via `nox.proactive.store.NotificationStore` (migration `0010_notifications`)
    so it survives a restart; `source`/`dismissed_at`/`expires_at` are additive fields every
    existing caller of `notify` can simply leave at their defaults."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: NotifyKind
    priority: str
    text: str
    channel: str
    spoken: bool = False
    announced: bool = False
    suppressed_reason: str = ""
    source: str = ""
    dismissed_at: datetime | None = None
    expires_at: datetime | None = None


class ProactiveStatus(BaseModel):
    """`proactive.status.read {}` tool response."""

    enabled: bool
    focus_mode: bool
    quiet_hours: bool
    effective_ceiling: int  # 0..5, the per-mode proactivity ceiling in force right now
    interruptions_used_this_hour: int
    interruptions_budget: int
    recent: list[NotificationRecord] = Field(default_factory=list)
