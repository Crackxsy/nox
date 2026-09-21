"""DefaultRouter: provider chain per role, privacy filter, health cache, fallbacks, events, budget.

Implements the ``Router`` contract of ``nox.ai.base`` and /,,. Budget (v0.1, in-memory; persistence
is v0.2): tokens are counted per day, role and locality. Background requests may not push the
*cloud* background share above ``ai.router.background_budget_share`` of today's cloud tokens; when
it is exceeded, cloud providers are skipped for background requests (local ones still run, marked
degraded) and the request is refused only if no local provider remains. Below
``budget_warmup_tokens`` cloud tokens the share is not enforced, so the first requests of the day
are never blocked.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import date
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from nox.ai._log import get_logger
from nox.ai.base import AiChunk, AiProvider, AiRequest, AiResponse, AiRole, ProviderInfo
from nox.ai.config import RouterConfig
from nox.ai.errors import (
    BudgetExceededError,
    NoProviderAvailableError,
    ProviderError,
    ProviderTimeoutError,
)
from nox.core.events import E, Event, EventBus, HealthStatus
from nox.util.aio import aclose

CLOUD_BLOCKING_MODES = frozenset({"private", "offline"})
log = get_logger(__name__)


@runtime_checkable
class _HasLastResponse(Protocol):
    def last_response(self, request_id: str) -> AiResponse | None: ...


class Decision(BaseModel):
    """One routing decision, kept for ``explain``."""

    request_id: str
    role: str
    privacy_mode: str
    chain: list[str]
    notes: list[str] = Field(default_factory=list)
    chosen: str | None = None
    degraded: bool = False
    degraded_reason: str = ""

    def render(self) -> str:
        head = (
            f"request {self.request_id} ({self.role}, privacy={self.privacy_mode}): "
            f"chain {' > '.join(self.chain) or '-'}"
        )
        lines = [head, *(f"  - {n}" for n in self.notes)]
        if self.chosen:
            suffix = f" (degraded: {self.degraded_reason})" if self.degraded else ""
            lines.append(f"  => {self.chosen}{suffix}")
        else:
            lines.append("  => no provider")
        return "\n".join(lines)


class _Usage(BaseModel):
    day: date
    by_role: dict[str, int] = Field(default_factory=dict)
    cloud_by_role: dict[str, int] = Field(default_factory=dict)

    def total(self) -> int:
        return sum(self.by_role.values())

    def cloud_total(self) -> int:
        return sum(self.cloud_by_role.values())


class DefaultRouter:
    """Router with health cache, privacy filter, fallbacks and budget share (module docstring)."""

    def __init__(
        self,
        providers: Sequence[AiProvider],
        bus: EventBus,
        config: RouterConfig | None = None,
        *,
        cloud_allowed: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
        max_decisions: int = 100,
    ) -> None:
        self._providers: dict[str, AiProvider] = {p.info.id: p for p in providers}
        self._bus = bus
        self._cfg = config or RouterConfig()
        self._cloud_allowed = cloud_allowed or (lambda: True)
        self._clock = clock
        self._today = today
        self._health: dict[str, tuple[float, ProviderInfo]] = {}
        self._decisions: deque[Decision] = deque(maxlen=max_decisions)
        self._usage = _Usage(day=today())

    # -- public extras --------------------------------------------------------------------------

    @property
    def config(self) -> RouterConfig:
        return self._cfg

    def usage_today(self) -> dict[str, dict[str, int]]:
        """Token counts for today: ``{"all": {role: n}, "cloud": {role: n}}``."""
        self._roll_day()
        return {"all": dict(self._usage.by_role), "cloud": dict(self._usage.cloud_by_role)}

    def decisions(self) -> list[Decision]:
        return list(self._decisions)

    # -- Router contract ------------------------------------------------------------------------

    async def providers(self) -> list[ProviderInfo]:
        infos = []
        for provider in self._providers.values():
            infos.append(await self._probe(provider))
        return infos

    def explain(self, request_id: str) -> str:
        for decision in reversed(self._decisions):
            if decision.request_id == request_id:
                return decision.render()
        return f"request {request_id}: no routing decision recorded"

    async def complete(self, request: AiRequest) -> AiResponse:
        candidates, decision = await self._select(request)
        timeout_s = request.timeout_s
        last_error: Exception | None = None
        for index, provider in enumerate(candidates):
            pid = provider.info.id
            await self._emit_started(request, pid)
            try:
                response = await asyncio.wait_for(provider.complete(request), timeout=timeout_s)
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:  # noqa: BLE001 - every provider failure must fall through
                last_error = self._as_error(exc, pid, timeout_s)
                self._mark_unavailable(provider, str(last_error))
                await self._emit_failed(
                    request, pid, str(last_error), candidates[index + 1 :], decision
                )
                continue
            response = self._finalize(response, decision, index, candidates)
            self._account(request.role, provider, response)
            await self._emit_ready(response)
            return response
        decision.notes.append("all candidates failed")
        self._decisions.append(decision)
        raise NoProviderAvailableError(str(last_error) if last_error else "chain exhausted")

    async def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:
        candidates, decision = await self._select(request)
        deadline = self._clock() + request.timeout_s
        last_error: Exception | None = None
        for index, provider in enumerate(candidates):
            pid = provider.info.id
            await self._emit_started(request, pid)
            parts: list[str] = []
            started = time.perf_counter()
            yielded = False
            iterator = provider.stream(request).__aiter__()
            try:
                while True:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise ProviderTimeoutError(pid, f"timeout after {request.timeout_s:.0f}s")
                    try:
                        chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
                    except StopAsyncIteration:
                        break
                    if chunk.delta:
                        parts.append(chunk.delta)
                        yielded = True
                    await self._emit_chunk(request, pid, chunk)
                    if chunk.done:
                        yield chunk
                        break
                    yield chunk
            except (asyncio.CancelledError, KeyboardInterrupt):
                await _aclose(iterator)
                raise
            except Exception as exc:  # noqa: BLE001 - see complete()
                await _aclose(iterator)
                last_error = self._as_error(exc, pid, request.timeout_s)
                self._mark_unavailable(provider, str(last_error))
                fallback_ok = not yielded
                # The providers after this one - never the one that just failed, and none at all
                # once chunks have already been delivered.
                remaining_chain = candidates[index + 1 :] if fallback_ok else []
                await self._emit_failed(request, pid, str(last_error), remaining_chain, decision)
                if not fallback_ok:
                    decision.notes.append(f"{pid}: failed after streaming started, no fallback")
                    self._decisions.append(decision)
                    raise NoProviderAvailableError(str(last_error)) from exc
                continue
            response = self._usage_after_stream(provider, request, parts, started)
            response = self._finalize(response, decision, index, candidates)
            self._account(request.role, provider, response)
            await self._emit_ready(response)
            return
        decision.notes.append("all candidates failed")
        self._decisions.append(decision)
        raise NoProviderAvailableError(str(last_error) if last_error else "chain exhausted")

    # -- selection ------------------------------------------------------------------------------

    async def _select(self, request: AiRequest) -> tuple[list[AiProvider], Decision]:
        chain = self._cfg.chain_for(request.role)
        decision = Decision(
            request_id=request.request_id,
            role=request.role.value,
            privacy_mode=request.privacy_mode,
            chain=chain,
        )
        cloud_blocked_reason = self._cloud_block_reason(request)
        budget_blocked = self._budget_block_reason(request)
        candidates: list[AiProvider] = []
        for pid in chain:
            provider = self._providers.get(pid)
            if provider is None:
                decision.notes.append(f"{pid}: skipped (not configured)")
                continue
            info = provider.info
            if request.role not in info.roles:
                decision.notes.append(f"{pid}: skipped (role {request.role.value} not supported)")
                continue
            if not info.local and cloud_blocked_reason:
                decision.notes.append(f"{pid}: skipped ({cloud_blocked_reason})")
                continue
            if not info.local and budget_blocked:
                decision.notes.append(f"{pid}: skipped ({budget_blocked})")
                continue
            health = await self._probe(provider)
            if health.status is HealthStatus.UNAVAILABLE:
                decision.notes.append(f"{pid}: skipped (unavailable: {health.reason})")
                continue
            candidates.append(provider)
        if not candidates:
            self._decisions.append(decision)
            error = budget_blocked or cloud_blocked_reason or "no provider available"
            await self._bus.publish(
                Event(
                    name=E.AI_REQUEST_FAILED,
                    payload={
                        "request_id": request.request_id,
                        "provider": "router",
                        "error": error,
                        "fallback_to": None,
                    },
                    corr=request.request_id,
                    source="ai.router",
                )
            )
            if budget_blocked and request.role is AiRole.BACKGROUND:
                raise BudgetExceededError(budget_blocked)
            raise NoProviderAvailableError(error)
        return candidates, decision

    def _cloud_block_reason(self, request: AiRequest) -> str:
        if request.privacy_mode in CLOUD_BLOCKING_MODES:
            return f"cloud blocked by privacy mode {request.privacy_mode}"
        if not self._cloud_allowed():
            return "cloud blocked by profile"
        return ""

    def _budget_block_reason(self, request: AiRequest) -> str:
        if request.role is not AiRole.BACKGROUND:
            return ""
        self._roll_day()
        cloud_total = self._usage.cloud_total()
        if cloud_total < self._cfg.budget_warmup_tokens:
            return ""
        share = self._usage.cloud_by_role.get(AiRole.BACKGROUND.value, 0) / cloud_total
        if share > self._cfg.background_budget_share:
            return (
                f"background cloud budget exhausted ({share:.0%} > "
                f"{self._cfg.background_budget_share:.0%})"
            )
        return ""

    async def _probe(self, provider: AiProvider) -> ProviderInfo:
        pid = provider.info.id
        now = self._clock()
        cached = self._health.get(pid)
        if cached is not None and now - cached[0] < self._cfg.health_ttl_s:
            return cached[1]
        try:
            info = await provider.health()
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - a broken health() must not break routing
            info = provider.info.model_copy(
                update={"status": HealthStatus.UNAVAILABLE, "reason": f"health failed: {exc}"}
            )
        previous = cached[1].status if cached else None
        self._health[pid] = (now, info)
        if previous is not None and previous != info.status:
            log.info("ai.provider_health", provider=pid, status=info.status, reason=info.reason)
        return info

    def _mark_unavailable(self, provider: AiProvider, reason: str) -> None:
        pid = provider.info.id
        info = provider.info.model_copy(
            update={"status": HealthStatus.UNAVAILABLE, "reason": reason[:200]}
        )
        self._health[pid] = (self._clock(), info)

    # -- bookkeeping ----------------------------------------------------------------------------

    def _finalize(
        self, response: AiResponse, decision: Decision, index: int, candidates: Sequence[AiProvider]
    ) -> AiResponse:
        first = decision.chain[0] if decision.chain else candidates[0].info.id
        degraded = response.degraded
        reason = response.degraded_reason
        chosen = candidates[index].info.id
        if chosen != first:
            degraded = True
            fallback = f"fallback:{first}->{chosen}"
            reason = f"{fallback};{reason}" if reason else fallback
        decision.chosen = chosen
        decision.degraded = degraded
        decision.degraded_reason = reason
        self._decisions.append(decision)
        return response.model_copy(update={"degraded": degraded, "degraded_reason": reason})

    def _usage_after_stream(
        self, provider: AiProvider, request: AiRequest, parts: list[str], started: float
    ) -> AiResponse:
        recorded = (
            provider.last_response(request.request_id)
            if isinstance(provider, _HasLastResponse)
            else None
        )
        text = "".join(parts)
        if recorded is not None:
            return recorded.model_copy(update={"text": recorded.text or text})
        return AiResponse(
            request_id=request.request_id,
            provider=provider.info.id,
            text=text,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _roll_day(self) -> None:
        today = self._today()
        if self._usage.day != today:
            self._usage = _Usage(day=today)

    def _account(self, role: AiRole, provider: AiProvider, response: AiResponse) -> None:
        self._roll_day()
        tokens = (response.tokens_in or 0) + (response.tokens_out or 0)
        if tokens == 0:
            tokens = max(1, len(response.text) // 4)
        key = role.value
        self._usage.by_role[key] = self._usage.by_role.get(key, 0) + tokens
        if not provider.info.local:
            self._usage.cloud_by_role[key] = self._usage.cloud_by_role.get(key, 0) + tokens

    @staticmethod
    def _as_error(exc: Exception, pid: str, timeout_s: float) -> Exception:
        if isinstance(exc, TimeoutError) and not isinstance(exc, ProviderError):
            return ProviderTimeoutError(pid, f"timeout after {timeout_s:.0f}s")
        return exc

    # -- events ---------------------------------------------------------------------------------

    async def _emit_started(self, request: AiRequest, pid: str) -> None:
        await self._bus.publish(
            Event(
                name=E.AI_REQUEST_STARTED,
                payload={
                    "request_id": request.request_id,
                    "provider": pid,
                    "role": request.role.value,
                    "mode": request.mode,
                },
                corr=request.request_id,
                source="ai.router",
            )
        )

    async def _emit_chunk(self, request: AiRequest, pid: str, chunk: AiChunk) -> None:
        await self._bus.publish(
            Event(
                name=E.AI_RESPONSE_CHUNK,
                payload={
                    "request_id": request.request_id,
                    "provider": pid,
                    "delta": chunk.delta,
                    "done": chunk.done,
                },
                corr=request.request_id,
                source="ai.router",
            )
        )

    async def _emit_ready(self, response: AiResponse) -> None:
        await self._bus.publish(
            Event(
                name=E.AI_RESPONSE_READY,
                payload=response.model_dump(mode="json"),
                corr=response.request_id,
                source="ai.router",
            )
        )

    async def _emit_failed(
        self,
        request: AiRequest,
        pid: str,
        error: str,
        remaining: Sequence[AiProvider],
        decision: Decision,
    ) -> None:
        """`remaining` is the chain *after* the failed provider; its head is the fallback."""
        fallback_to = remaining[0].info.id if remaining else None
        decision.notes.append(f"{pid}: failed ({error[:120]})")
        await self._bus.publish(
            Event(
                name=E.AI_REQUEST_FAILED,
                payload={
                    "request_id": request.request_id,
                    "provider": pid,
                    "error": error,
                    "fallback_to": fallback_to,
                },
                corr=request.request_id,
                source="ai.router",
            )
        )
        if fallback_to is not None:
            await self._bus.publish(
                Event(
                    name=E.AI_PROVIDER_CHANGED,
                    payload={
                        "request_id": request.request_id,
                        "previous": pid,
                        "current": fallback_to,
                        "reason": error,
                    },
                    corr=request.request_id,
                    source="ai.router",
                )
            )


async def _aclose(iterator: AsyncIterator[AiChunk]) -> None:
    """Close an abandoned provider stream without masking the error that abandoned it."""
    try:
        await aclose(iterator)
    except Exception:  # noqa: BLE001 - closing a broken generator must not mask the error
        log.debug("ai.router.aclose_failed")
