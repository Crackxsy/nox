"""AsyncEventBus: the in-process pub/sub implementing `nox.core.events.EventBus` (Event Model,
ADR-001).

Delivery model (deliberate choice): `publish` validates the payload, then awaits delivery to every
matching handler *in the caller's task*, sequentially, in priority order. There is no dispatcher
task, so ordering is deterministic, tests need no background loop, and a publisher knows its event
was handled when `publish` returns. Consequences: a slow handler delays its publisher (handlers
must not block; offload with tasks), and a handler that publishes from inside a handler nests
inline.

Lanes: `security.*` and `system.*` are the priority lane - delivered immediately, never subject to
the capacity limit. Every other event is the normal lane, bounded by `capacity` (default 10 000)
concurrent publishes: when full, `stream.*`/`sensor.*` events evict the oldest still-waiting event
of those families to make room for themselves (if none is waiting yet, they simply join the wait
queue like any other event, themselves droppable by a later arrival); all other events wait
(backpressure).

Subscriptions are glob patterns: `*` matches one segment (and, as the sole pattern, everything -
the contract docstring lists "*" as the catch-all), `**` matches one or more segments, `*` inside a
segment matches within it (`voice.transcript_*`). Handler errors are caught, logged, counted in
`stats()` and emitted as `system.handler_failed`.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any

from pydantic import BaseModel

from nox.core.events import E, Event, Handler, validate_payload
from nox.core.logging import get_logger

PRIORITY_DOMAINS: frozenset[str] = frozenset({"security", "system"})
DROPPABLE_DOMAINS: frozenset[str] = frozenset({"stream", "sensor"})
DEFAULT_CAPACITY = 10_000


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Translate a dotted glob into a regex. See module docstring for the rules."""
    if not pattern:
        raise ValueError("empty subscription pattern")
    if pattern in ("*", "**"):
        return re.compile(r"^.+$")
    parts: list[str] = []
    for segment in pattern.split("."):
        if segment == "**":
            parts.append(r"[^.]+(?:\.[^.]+)*")
        elif segment == "":
            raise ValueError(f"empty segment in pattern {pattern!r}")
        else:
            parts.append(re.escape(segment).replace(r"\*", r"[^.]*"))
    return re.compile("^" + r"\.".join(parts) + "$")


def domain_of(name: str) -> str:
    return name.split(".", 1)[0]


class BusStats(BaseModel):
    published: int = 0
    delivered: int = 0
    dropped: int = 0
    handler_errors: int = 0
    in_flight: int = 0
    waiting: int = 0
    subscriptions: int = 0
    errors_by_handler: dict[str, int] = {}


@dataclass(slots=True)
class _Subscription:
    id: int
    pattern: str
    regex: re.Pattern[str]
    handler: Handler
    priority: bool  # pattern targets a priority domain -> handler runs before others
    name: str = field(default="")


def _handler_name(handler: Handler) -> str:
    qual = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", None)
    module = getattr(handler, "__module__", "") or ""
    if qual is None:
        return repr(handler)
    return f"{module}.{qual}" if module else str(qual)


class AsyncEventBus:
    """See module docstring. Safe to use from any task of one event loop (not thread-safe)."""

    def __init__(
        self, *, capacity: int = DEFAULT_CAPACITY, emit_handler_failures: bool = True
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._emit_handler_failures = emit_handler_failures
        self._subs: OrderedDict[int, _Subscription] = OrderedDict()
        self._ids = count(1)
        self._log = get_logger(__name__)
        self._in_flight = 0
        # FIFO of waiting normal-lane publishers: future -> droppable
        self._waiting: OrderedDict[asyncio.Future[bool], bool] = OrderedDict()
        self._published = 0
        self._delivered = 0
        self._dropped = 0
        self._handler_errors = 0
        self._errors_by_handler: dict[str, int] = {}

    # ---- subscriptions -----------------------------------------------------------------------

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        regex = compile_pattern(pattern)
        sub_id = next(self._ids)
        priority = domain_of(pattern) in PRIORITY_DOMAINS
        sub = _Subscription(sub_id, pattern, regex, handler, priority, _handler_name(handler))
        self._subs[sub_id] = sub

        def unsubscribe() -> None:
            self._subs.pop(sub_id, None)

        return unsubscribe

    def _matching(self, name: str) -> list[_Subscription]:
        matches = [s for s in self._subs.values() if s.regex.match(name)]
        # stable: priority-domain handlers first, then subscription order
        return sorted(matches, key=lambda s: (0 if s.priority else 1, s.id))

    # ---- publishing --------------------------------------------------------------------------

    async def publish(self, event: Event) -> None:
        validated = validate_payload(event.name, event.payload)  # raises for invalid payloads (bug)
        if validated is not event.payload:
            event = event.model_copy(update={"payload": validated})
        self._published += 1

        if domain_of(event.name) in PRIORITY_DOMAINS:
            await self._deliver(event)
            return

        if not await self._acquire_slot(event):
            return
        try:
            await self._deliver(event)
        finally:
            self._release_slot()

    async def _acquire_slot(self, event: Event) -> bool:
        """Normal lane admission. Returns False when the event was dropped."""
        droppable = domain_of(event.name) in DROPPABLE_DOMAINS
        if self._in_flight + len(self._waiting) < self._capacity:
            self._in_flight += 1
            return True
        if droppable:
            victim = next((fut for fut, drop in self._waiting.items() if drop), None)
            if victim is not None:
                self._waiting.pop(victim)
                if not victim.done():
                    victim.set_result(False)  # tell that publisher it was dropped
                self._dropped += 1
                self._log.debug(
                    "bus.dropped", event_name=event.name, reason="capacity, dropped oldest"
                )
            # else: nothing older and droppable to evict yet - this event joins the queue
            # itself (still marked droppable, so a later arrival can evict it in its place).
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._waiting[fut] = droppable
        try:
            admitted = await fut
        except asyncio.CancelledError:
            self._waiting.pop(fut, None)
            raise
        if admitted:
            self._in_flight += 1
        return admitted

    def _release_slot(self) -> None:
        self._in_flight -= 1
        while self._waiting:
            fut, _ = self._waiting.popitem(last=False)
            if not fut.done():
                fut.set_result(True)
                return

    async def _deliver(self, event: Event) -> None:
        for sub in self._matching(event.name):
            if sub.id not in self._subs:  # unsubscribed by an earlier handler in this round
                continue
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    await result
                self._delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolation: one handler never breaks the others
                await self._handler_failed(event, sub, exc)

    async def _handler_failed(self, event: Event, sub: _Subscription, exc: BaseException) -> None:
        self._handler_errors += 1
        self._errors_by_handler[sub.name] = self._errors_by_handler.get(sub.name, 0) + 1
        self._log.error(
            "bus.handler_failed",
            event_name=event.name,
            handler=sub.name,
            pattern=sub.pattern,
            error=f"{type(exc).__name__}: {exc}",
        )
        if self._emit_handler_failures and event.name != E.SYSTEM_HANDLER_FAILED:
            failure = Event(
                name=E.SYSTEM_HANDLER_FAILED,
                payload={
                    "event": event.name,
                    "handler": sub.name,
                    "pattern": sub.pattern,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                corr=event.corr,
                source="bus",
            )
            await self._deliver(failure)

    # ---- request-style helpers ---------------------------------------------------------------

    async def wait_for(
        self, name: str, *, timeout: float | None = None, corr: str | None = None
    ) -> Event:
        """Wait for the next event matching `name` (glob allowed) and, if given, `corr`."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Event] = loop.create_future()

        def _catch(event: Event) -> None:
            if fut.done():
                return
            if corr is not None and event.corr != corr:
                return
            fut.set_result(event)

        unsubscribe = self.subscribe(name, _catch)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            unsubscribe()

    # ---- introspection -----------------------------------------------------------------------

    def stats(self) -> BusStats:
        return BusStats(
            published=self._published,
            delivered=self._delivered,
            dropped=self._dropped,
            handler_errors=self._handler_errors,
            in_flight=self._in_flight,
            waiting=len(self._waiting),
            subscriptions=len(self._subs),
            errors_by_handler=dict(self._errors_by_handler),
        )

    def subscriptions(self) -> list[tuple[str, str]]:
        """(pattern, handler name) pairs, in subscription order. For dashboards and tests."""
        return [(s.pattern, s.name) for s in self._subs.values()]

    def __len__(self) -> int:
        return len(self._subs)

    def __repr__(self) -> str:
        return f"AsyncEventBus(subs={len(self._subs)}, capacity={self._capacity})"


def payload_dict(model: BaseModel) -> dict[str, Any]:
    """Convenience: dump a payload model to the JSON-compatible dict an Event carries."""
    return model.model_dump(mode="json")
