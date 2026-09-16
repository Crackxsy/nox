"""FunkenService: Funken v1 ledger - earn/spend/balance/tier (Spec v0.2 §3.5, §7 `funken_ledger`,
§9 `twitch.funken.award`/`twitch.funken.admin_adjust`).

Deterministic, no LLM: every change is a plain arithmetic ledger append via `FunkenLedgerRepository`
/`ViewerRepository` (`nox.data.stream_repos`) and an audited `stream.funken_changed` event
(`StreamFunkenChanged`, `nox.core.events`). Rates and tier thresholds come from
`config.stream.funken.*` (placeholder numbers, pending approval per Spec v0.2 §3.5). Permission
checks (owner-only `admin_adjust`, per-viewer rate limits on `earn`) are the caller's job - this
service is the ledger, not the permission engine; a `ToolExecutor`-backed `twitch.funken.*` tool
wraps it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from nox.core.config import FunkenConfig
from nox.core.events import E, Event, EventBus, StreamFunkenChanged
from nox.core.logging import get_logger
from nox.data.stream_repos import FunkenLedgerRepository, ViewerRepository
from nox.security.model import AuditLog

log = get_logger(__name__)

Clock = Callable[[], datetime]

# `earn()` cooldown (anti-farming, FR-9.17) applies to chat activity only; subs/bits/raids are
# rate-limited by Twitch itself and are always honoured.
_EARN_RATE_LIMITED_REASONS: frozenset[str] = frozenset({"message"})


class FunkenChangeResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    viewer_id: str
    delta: float
    balance_after: float
    reason: str = ""
    source: str = "earn"  # earn | spend | admin | decay
    tier: str = "none"
    skipped: bool = False
    skip_reason: str = ""


class InsufficientFunkenError(ValueError):
    """`spend()` was asked to deduct more than the viewer's current balance."""


class FunkenService:
    def __init__(
        self,
        viewers: ViewerRepository,
        ledger: FunkenLedgerRepository,
        config: FunkenConfig,
        *,
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._viewers = viewers
        self._ledger = ledger
        self._config = config
        self._bus = bus
        self._audit = audit
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._last_earn: dict[str, datetime] = {}

    # ---- reads -------------------------------------------------------------------------------

    def balance(self, viewer_id: str) -> float:
        viewer = self._viewers.get(viewer_id)
        return viewer.funken_balance if viewer is not None else 0.0

    def tier(self, viewer_id: str) -> str:
        return self._tier_for_balance(self.balance(viewer_id))

    def _tier_for_balance(self, balance: float) -> str:
        names = self._config.tier_names
        thresholds = self._config.tier_thresholds
        current = "none"
        for name, threshold in zip(names, thresholds, strict=True):
            if balance >= threshold:
                current = name
        return current

    def _rate_for_reason(self, reason: str) -> float:
        rates = {
            "message": self._config.earn_per_message,
            "sub": self._config.earn_per_sub,
            "bit": self._config.earn_per_bit,
            "raid": self._config.earn_per_raid,
        }
        return rates.get(reason, 0.0)

    # ---- writes ------------------------------------------------------------------------------

    async def earn(
        self, viewer_id: str, reason: str, *, amount: float | None = None
    ) -> FunkenChangeResult:
        """Award Funken for a viewer-activity event (`reason`: message|sub|bit|raid|...).

        Rate-limited per viewer for `reason="message"` (anti-farming); other reasons always earn.
        """
        now = self._clock()
        if reason in _EARN_RATE_LIMITED_REASONS:
            last = self._last_earn.get(viewer_id)
            if last is not None and (now - last) < timedelta(seconds=self._config.earn_cooldown_s):
                return FunkenChangeResult(
                    viewer_id=viewer_id,
                    delta=0.0,
                    balance_after=self.balance(viewer_id),
                    reason=reason,
                    source="earn",
                    tier=self.tier(viewer_id),
                    skipped=True,
                    skip_reason="cooldown",
                )
            self._last_earn[viewer_id] = now
        delta = self._rate_for_reason(reason) if amount is None else amount
        return await self._apply(viewer_id, delta, reason=reason, source="earn", now=now)

    async def spend(self, viewer_id: str, amount: float, *, reason: str) -> FunkenChangeResult:
        """Deduct Funken for a pet interaction/minigame entry. Raises when the balance would go
        negative - deterministic, no partial spends."""
        if amount < 0:
            raise ValueError("spend amount must be >= 0")
        current = self.balance(viewer_id)
        if amount > current:
            raise InsufficientFunkenError(
                f"viewer {viewer_id!r} has {current:g} Funken, cannot spend {amount:g}"
            )
        now = self._clock()
        return await self._apply(viewer_id, -amount, reason=reason, source="spend", now=now)

    async def admin_adjust(
        self, viewer_id: str, delta: float, *, reason: str, by: str
    ) -> FunkenChangeResult:
        """Owner-only balance correction (Spec v0.2 §3.5.4, `twitch.funken.admin_adjust`, `high`
        risk/`confirm` - enforced by the caller's `ToolExecutor`, not here)."""
        now = self._clock()
        result = await self._apply(viewer_id, delta, reason=reason, source="admin", now=now)
        if self._audit is not None:
            self._audit.append(
                actor=by,
                tool="twitch",
                action="funken.admin_adjust",
                target=viewer_id,
                decision="allow",
                result="ok",
                details={"delta": f"{delta:g}", "balance_after": f"{result.balance_after:g}"},
            )
        return result

    async def _apply(
        self, viewer_id: str, delta: float, *, reason: str, source: str, now: datetime
    ) -> FunkenChangeResult:
        balance_after = self.balance(viewer_id) + delta
        self._viewers.set_balance(viewer_id, balance_after)
        tier = self._tier_for_balance(balance_after)
        self._viewers.set_tier(viewer_id, tier)
        self._ledger.append(
            viewer_id, delta, balance_after=balance_after, reason=reason, source=source, ts=now
        )
        log.info(
            "stream.funken_changed",
            viewer_id=viewer_id,
            delta=delta,
            source=source,
            balance_after=balance_after,
        )
        if self._bus is not None:
            payload = StreamFunkenChanged(
                viewer_id=viewer_id,
                delta=delta,
                reason=reason,
                balance_after=balance_after,
                source=source,
                tier=tier,
            )
            await self._bus.publish(
                Event(name=E.STREAM_FUNKEN_CHANGED, payload=payload.model_dump(mode="json"))
            )
        return FunkenChangeResult(
            viewer_id=viewer_id,
            delta=delta,
            balance_after=balance_after,
            reason=reason,
            source=source,
            tier=tier,
        )
