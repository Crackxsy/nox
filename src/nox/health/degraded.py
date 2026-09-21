"""Degraded-mode matrix (Health & Recovery): watches `system.health_changed` for a fixed set of
critical components, keeps `NoxState.system.level` honest (`degraded` while any of them is not
`available`, `running` again once they all recover), and attempts a rate-limited self-repair action
per component before falling back to just reporting the degradation.

Never touches `safe_mode`/`stopping` - those are the security path (kill switch) and shutdown, both
outside this service's authority; it only ever moves between `running` and `degraded`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from nox.core.events import E, Event, EventBus, HealthStatus
from nox.core.logging import get_logger
from nox.core.state import StateManager, SystemLevel

log = get_logger(__name__)

#: Component name this service publishes its own level changes under. It is never treated as an
#: incoming health signal - see _on_health_changed.
_SYSTEM_LEVEL_COMPONENT = "system.level"

#: The Failure and Recovery Model rows this service reacts to (disk full, vault unreachable,
#: audit chain broken, config invalid - see `nox.health.checks`).
DEFAULT_CRITICAL_COMPONENTS: frozenset[str] = frozenset(
    {"system.disk", "memory.vault", "security.audit_chain", "system.config"}
)

RepairAction = Callable[[], Awaitable[bool]]
EscalateAction = Callable[[str], Awaitable[None]]


@dataclass
class SelfRepairPolicy:
    max_attempts: int = 3
    window_s: float = 3600.0


class Clock(Protocol):
    def __call__(self) -> float: ...


@dataclass
class SelfRepair:
    """Rate-limited dispatch to a `RepairAction` per component (ST-09's "self-repair actions...
    with limits"): at most `policy.max_attempts` tries within `policy.window_s`, then it gives up
    and lets the degraded state stand until the component recovers on its own or a human acts."""

    actions: dict[str, RepairAction] = field(default_factory=dict)
    policy: SelfRepairPolicy = field(default_factory=SelfRepairPolicy)
    clock: Callable[[], float] = field(default=time.monotonic)
    _attempts: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)

    async def try_repair(self, component: str) -> bool:
        action = self.actions.get(component)
        if action is None:
            return False
        now = self.clock()
        history = [t for t in self._attempts.get(component, []) if now - t <= self.policy.window_s]
        if len(history) >= self.policy.max_attempts:
            log.warning("health.repair_limit_reached", component=component, attempts=len(history))
            return False
        history.append(now)
        self._attempts[component] = history
        try:
            ok = await action()
        except Exception as exc:  # noqa: BLE001 - a failed repair is a finding, not a crash
            log.error("health.repair_failed", component=component, error=str(exc))
            return False
        log.info("health.repair_attempted", component=component, ok=ok)
        return ok


class DegradedModeService:
    def __init__(
        self,
        bus: EventBus,
        state: StateManager,
        *,
        critical_components: frozenset[str] = DEFAULT_CRITICAL_COMPONENTS,
        repair: SelfRepair | None = None,
        escalate: dict[str, EscalateAction] | None = None,
    ) -> None:
        self._bus = bus
        self._state = state
        self._critical = critical_components
        self._repair = repair
        self._escalate = escalate or {}
        self._bad: set[str] = set()
        self._unsub: Callable[[], None] | None = None

    def start(self) -> None:
        if self._unsub is None:
            self._unsub = self._bus.subscribe(E.SYSTEM_HEALTH_CHANGED, self._on_health_changed)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @property
    def degraded_components(self) -> frozenset[str]:
        return frozenset(self._bad)

    async def _on_health_changed(self, event: Event) -> None:
        component = str(event.payload.get("component", ""))
        if component == _SYSTEM_LEVEL_COMPONENT:
            # Our own announcement, coming back through the bus. Reacting to it would make this
            # service its own input; the exclusion is here rather than left to whoever configures
            # `critical_components`.
            return
        if component not in self._critical:
            return
        status = str(event.payload.get("status", ""))
        was_bad = component in self._bad
        is_bad = status != HealthStatus.AVAILABLE.value
        if is_bad:
            self._bad.add(component)
            if not was_bad:
                await self._respond(component)
        else:
            self._bad.discard(component)
        await self._apply_level()

    async def _respond(self, component: str) -> None:
        escalate = self._escalate.get(component)
        if escalate is not None:
            await escalate(component)
            return
        if self._repair is not None:
            await self._repair.try_repair(component)

    async def _apply_level(self) -> None:
        current = self._state.get("system.level")
        current_value = current.value if isinstance(current, SystemLevel) else str(current)
        if current_value not in (SystemLevel.RUNNING.value, SystemLevel.DEGRADED.value):
            return  # safe_mode/stopping outrank the degraded-mode matrix
        target = SystemLevel.DEGRADED if self._bad else SystemLevel.RUNNING
        if current_value == target.value:
            return
        await self._state.update("system.level", target.value, reason="degraded_mode")
        await self._bus.publish(
            Event(
                name=E.SYSTEM_HEALTH_CHANGED,
                payload={
                    "component": _SYSTEM_LEVEL_COMPONENT,
                    "status": HealthStatus.LIMITED.value
                    if target is SystemLevel.DEGRADED
                    else HealthStatus.AVAILABLE.value,
                    "reason": ",".join(sorted(self._bad)),
                },
                source="health.degraded",
            )
        )
