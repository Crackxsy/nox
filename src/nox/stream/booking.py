"""FunkenBooking: bridges Stream Bot events to `FunkenService` (Spec v0.2 §3.5/§8/§9, EPIC-11).

Plugins never book Funken themselves (see `nox.stream.funken` module docstring) - they only
publish `stream.funken_awarded` requests; this is the core-side subscriber that turns those into
ledger earns, gated by a per-viewer daily cap (`stream.funken.earn_daily_cap`) and by "only while a
stream session is active" (no farming Funken outside a stream). It also answers the viewer-facing
`!funken` chat command (`twitch.command_invoked` with `command="funken"`) - exclusively through
`ToolExecutor.call("twitch.chat.send", ...)`, never by talking to the Twitch plugin directly - and
serves the `stream.funken.top` IPC request (leaderboard).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from nox.core.config import FunkenConfig
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.data.stream_repos import ViewerRepository
from nox.stream.funken import FunkenService
from nox.tools.executor import ToolExecutor

log = get_logger(__name__)

Clock = Callable[[], datetime]
Unsubscribe = Callable[[], None]


class FunkenBooking:
    def __init__(
        self,
        bus: EventBus,
        funken: FunkenService,
        viewers: ViewerRepository,
        executor: ToolExecutor,
        config: FunkenConfig,
        *,
        is_session_active: Callable[[], bool],
        clock: Clock | None = None,
    ) -> None:
        self._bus = bus
        self._funken = funken
        self._viewers = viewers
        self._executor = executor
        self._config = config
        self._is_active = is_session_active
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        # viewer_id -> (ISO date, Funken earned that day); reset lazily when the date rolls over.
        self._daily_earned: dict[str, tuple[str, float]] = {}
        self._unsubs: list[Unsubscribe] = []

    def start(self) -> None:
        self._unsubs = [
            self._bus.subscribe(E.STREAM_FUNKEN_AWARDED, self._on_awarded),
            self._bus.subscribe(E.TWITCH_COMMAND_INVOKED, self._on_command),
        ]

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    # ---- daily cap bookkeeping ------------------------------------------------------------------

    def _today(self) -> str:
        return self._clock().date().isoformat()

    def _earned_today(self, viewer_id: str) -> float:
        entry = self._daily_earned.get(viewer_id)
        if entry is None or entry[0] != self._today():
            return 0.0
        return entry[1]

    def _record_earned(self, viewer_id: str, amount: float) -> None:
        self._daily_earned[viewer_id] = (self._today(), self._earned_today(viewer_id) + amount)

    # ---- event handlers ---------------------------------------------------------------------

    async def _on_awarded(self, ev: Event) -> None:
        if not self._is_active():
            log.debug("stream.funken_award_ignored", reason="no_active_session")
            return
        viewer_id = str(ev.payload.get("viewer_id", ""))
        if not viewer_id:
            return
        reason = str(ev.payload.get("reason", ""))
        delta = float(ev.payload.get("delta", 0.0))
        if delta <= 0:
            return
        cap = self._config.earn_daily_cap
        if cap > 0:
            remaining = cap - self._earned_today(viewer_id)
            if remaining <= 0:
                log.info("stream.funken_daily_cap_reached", viewer_id=viewer_id)
                return
            delta = min(delta, remaining)
        result = await self._funken.earn(viewer_id, reason, amount=delta)
        if not result.skipped and cap > 0 and result.delta > 0:
            self._record_earned(viewer_id, result.delta)

    async def _on_command(self, ev: Event) -> None:
        if str(ev.payload.get("command", "")) != "funken":
            return
        viewer_id = str(ev.payload.get("viewer_id", ""))
        if not viewer_id:
            return
        balance = self._funken.balance(viewer_id)
        tier = self._funken.tier(viewer_id)
        text = f"Du hast {balance:g} Funken (Stufe: {tier})."
        await self._executor.call(
            agent="nox.stream",
            name="twitch.chat.send",
            arguments={"text": text},
            mode="stream",
        )

    # ---- reads --------------------------------------------------------------------------------

    def top(self, limit: int = 10) -> list[dict[str, object]]:
        """`stream.funken.top {limit}` response body (roles shell/dashboard, see `nox.app`)."""
        rows = self._viewers.list_top_by_balance(limit)
        return [
            {
                "viewer_id": r.twitch_user_id,
                "display_name": r.display_name,
                "balance": r.funken_balance,
                "tier": r.loyalty_tier,
            }
            for r in rows
        ]
