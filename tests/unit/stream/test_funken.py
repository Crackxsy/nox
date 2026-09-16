"""nox.stream.funken.FunkenService: earn/spend/balance/tier, anti-farming cooldown, admin_adjust
audit, deterministic ledger, `stream.funken_changed` event."""

from __future__ import annotations

from datetime import timedelta

import pytest

from nox.core.config import FunkenConfig
from nox.core.events import E
from nox.data.stream_repos import FunkenLedgerRepository, ViewerRepository
from nox.security.audit import SqliteAuditLog
from nox.stream.funken import FunkenService, InsufficientFunkenError
from tests.unit.fakes import FakeBus
from tests.unit.stream.conftest import NOW, MutableClock


def make_service(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
    audit: SqliteAuditLog | None = None,
) -> FunkenService:
    viewers.touch("v1", "Alice", seen_at=NOW)
    return FunkenService(viewers, ledger, config, bus=bus, audit=audit, clock=clock)


async def test_earn_message_applies_configured_rate(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    funken_config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
) -> None:
    service = make_service(viewers, ledger, funken_config, bus, clock)
    result = await service.earn("v1", "message")
    assert result.skipped is False
    assert result.delta == funken_config.earn_per_message
    assert result.balance_after == funken_config.earn_per_message
    assert service.balance("v1") == funken_config.earn_per_message
    assert ledger.list_for_viewer("v1")[0].source == "earn"


async def test_earn_message_is_rate_limited_per_viewer(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    funken_config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
) -> None:
    service = make_service(viewers, ledger, funken_config, bus, clock)
    first = await service.earn("v1", "message")
    assert first.skipped is False
    clock.advance(1.0)  # well inside the cooldown window
    second = await service.earn("v1", "message")
    assert second.skipped is True
    assert second.skip_reason == "cooldown"
    assert second.delta == 0.0
    assert service.balance("v1") == funken_config.earn_per_message  # unchanged

    clock.advance(funken_config.earn_cooldown_s)
    third = await service.earn("v1", "message")
    assert third.skipped is False
    assert service.balance("v1") == 2 * funken_config.earn_per_message


async def test_earn_sub_bit_raid_are_never_rate_limited(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    funken_config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
) -> None:
    service = make_service(viewers, ledger, funken_config, bus, clock)
    first = await service.earn("v1", "sub")
    second = await service.earn("v1", "sub")  # immediately again, still not rate-limited
    assert first.skipped is False and second.skipped is False
    assert service.balance("v1") == 2 * funken_config.earn_per_sub


async def test_earn_explicit_amount_overrides_configured_rate(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    funken_config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
) -> None:
    service = make_service(viewers, ledger, funken_config, bus, clock)
    result = await service.earn("v1", "bits_cheer", amount=7.5)
    assert result.delta == 7.5
    assert service.balance("v1") == 7.5


async def test_spend_deducts_and_rejects_overdraft(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    funken_config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
) -> None:
    service = make_service(viewers, ledger, funken_config, bus, clock)
    await service.earn("v1", "sub")  # balance = earn_per_sub
    result = await service.spend("v1", 5.0, reason="pet_interaction")
    assert result.delta == -5.0
    assert service.balance("v1") == funken_config.earn_per_sub - 5.0

    with pytest.raises(InsufficientFunkenError):
        await service.spend("v1", 10_000.0, reason="pet_interaction")


async def test_admin_adjust_is_audited_and_applies_delta(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    funken_config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
    audit: SqliteAuditLog,
) -> None:
    service = make_service(viewers, ledger, funken_config, bus, clock, audit=audit)
    result = await service.admin_adjust("v1", 100.0, reason="giveaway", by="admin")
    assert result.balance_after == 100.0
    entries = audit.entries()
    assert len(entries) == 1
    assert entries[0].actor == "admin"
    assert entries[0].action == "funken.admin_adjust"
    assert entries[0].target == "v1"
    # admin can also go negative (correction)
    result2 = await service.admin_adjust("v1", -30.0, reason="correction", by="admin")
    assert result2.balance_after == 70.0
    assert len(audit.entries()) == 2


async def test_tier_thresholds(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    funken_config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
) -> None:
    service = make_service(viewers, ledger, funken_config, bus, clock)
    assert service.tier("v1") == "newcomer"
    await service.admin_adjust("v1", 100.0, reason="test", by="admin")
    assert service.tier("v1") == "regular"
    await service.admin_adjust("v1", 10_000.0, reason="test", by="admin")
    assert service.tier("v1") == "legend"


async def test_stream_funken_changed_event_published(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    funken_config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
) -> None:
    service = make_service(viewers, ledger, funken_config, bus, clock)
    await service.earn("v1", "sub")
    events = [e for e in bus.published if e.name == E.STREAM_FUNKEN_CHANGED]
    assert len(events) == 1
    assert events[0].payload["viewer_id"] == "v1"
    assert events[0].payload["source"] == "earn"
    assert events[0].payload["balance_after"] == funken_config.earn_per_sub


async def test_deterministic_no_randomness(
    viewers: ViewerRepository,
    ledger: FunkenLedgerRepository,
    funken_config: FunkenConfig,
    bus: FakeBus,
    clock: MutableClock,
) -> None:
    service = make_service(viewers, ledger, funken_config, bus, clock)
    clock.advance(1000.0)
    first = await service.earn("v1", "sub")
    clock.advance(1000.0)
    second = await service.earn("v1", "sub")
    assert first.delta == second.delta == funken_config.earn_per_sub
    rows = ledger.list_for_viewer("v1")
    assert rows[0].ts - rows[1].ts == timedelta(seconds=1000.0)
