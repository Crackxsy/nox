"""nox.health.degraded.DegradedModeService/SelfRepair: system.level transitions, rate-limited
self-repair, escalation for non-repairable rows (audit chain)."""

from __future__ import annotations

import pytest

from nox.core.bus import AsyncEventBus
from nox.core.events import E, Event, HealthStatus
from nox.core.statemgr import NoxStateManager
from nox.health.degraded import DegradedModeService, SelfRepair, SelfRepairPolicy


@pytest.fixture
def bus() -> AsyncEventBus:
    return AsyncEventBus()


@pytest.fixture
def state(bus: AsyncEventBus) -> NoxStateManager:
    return NoxStateManager(bus)


async def _health_changed(bus: AsyncEventBus, component: str, status: HealthStatus) -> None:
    await bus.publish(
        Event(
            name=E.SYSTEM_HEALTH_CHANGED,
            payload={"component": component, "status": status.value},
            source="test",
        )
    )


async def test_degraded_on_bad_component_and_recovers(
    bus: AsyncEventBus, state: NoxStateManager
) -> None:
    svc = DegradedModeService(bus, state, critical_components=frozenset({"system.disk"}))
    svc.start()
    assert state.get("system.level") == "running"
    await _health_changed(bus, "system.disk", HealthStatus.UNAVAILABLE)
    assert state.get("system.level") == "degraded"
    assert "system.disk" in svc.degraded_components
    await _health_changed(bus, "system.disk", HealthStatus.AVAILABLE)
    assert state.get("system.level") == "running"
    assert svc.degraded_components == frozenset()
    svc.stop()


async def test_ignores_non_critical_components(bus: AsyncEventBus, state: NoxStateManager) -> None:
    svc = DegradedModeService(bus, state, critical_components=frozenset({"system.disk"}))
    svc.start()
    await _health_changed(bus, "ai.rules", HealthStatus.UNAVAILABLE)
    assert state.get("system.level") == "running"
    svc.stop()


async def test_self_repair_invoked_and_rate_limited(
    bus: AsyncEventBus, state: NoxStateManager
) -> None:
    calls = {"n": 0}

    async def repair() -> bool:
        calls["n"] += 1
        return True

    repair_obj = SelfRepair(
        actions={"system.disk": repair}, policy=SelfRepairPolicy(max_attempts=1, window_s=3600)
    )
    svc = DegradedModeService(
        bus, state, critical_components=frozenset({"system.disk"}), repair=repair_obj
    )
    svc.start()
    await _health_changed(bus, "system.disk", HealthStatus.UNAVAILABLE)
    await _health_changed(bus, "system.disk", HealthStatus.AVAILABLE)
    await _health_changed(bus, "system.disk", HealthStatus.UNAVAILABLE)  # second failure, limit=1
    assert calls["n"] == 1
    svc.stop()


async def test_escalation_used_instead_of_repair(
    bus: AsyncEventBus, state: NoxStateManager
) -> None:
    escalated = []

    async def escalate(component: str) -> None:
        escalated.append(component)

    svc = DegradedModeService(
        bus,
        state,
        critical_components=frozenset({"security.audit_chain"}),
        escalate={"security.audit_chain": escalate},
    )
    svc.start()
    await _health_changed(bus, "security.audit_chain", HealthStatus.UNAVAILABLE)
    assert escalated == ["security.audit_chain"]
    svc.stop()


async def test_safe_mode_outranks_degraded_matrix(
    bus: AsyncEventBus, state: NoxStateManager
) -> None:
    await state.update("system.level", "safe_mode", reason="test")
    svc = DegradedModeService(bus, state, critical_components=frozenset({"system.disk"}))
    svc.start()
    await _health_changed(bus, "system.disk", HealthStatus.UNAVAILABLE)
    assert state.get("system.level") == "safe_mode"
    svc.stop()
