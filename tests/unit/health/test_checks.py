"""nox.health.checks: the four Failure and Recovery Model rows HealthService did not yet cover."""

from __future__ import annotations

from pathlib import Path

from nox.core.events import HealthStatus
from nox.health.checks import (
    make_audit_chain_check,
    make_config_check,
    make_disk_full_check,
    make_vault_unreachable_check,
)


class FakeAudit:
    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def verify_chain(self) -> bool:
        return self.ok


async def test_disk_full_check_available_with_plenty_of_space(tmp_path: Path) -> None:
    check = make_disk_full_check(tmp_path, warn_free_mb=1.0, critical_free_mb=0.1)
    status, reason = await check.probe()
    assert status is HealthStatus.AVAILABLE
    assert reason


async def test_disk_full_check_unavailable_below_critical(tmp_path: Path) -> None:
    check = make_disk_full_check(tmp_path, warn_free_mb=10**9, critical_free_mb=10**9)
    status, _reason = await check.probe()
    assert status is HealthStatus.UNAVAILABLE


async def test_disk_full_check_limited_between_warn_and_critical(tmp_path: Path) -> None:
    check = make_disk_full_check(tmp_path, warn_free_mb=10**9, critical_free_mb=0.1)
    status, _reason = await check.probe()
    assert status is HealthStatus.LIMITED


async def test_vault_unreachable_check_missing_dir(tmp_path: Path) -> None:
    check = make_vault_unreachable_check(tmp_path / "does-not-exist")
    status, reason = await check.probe()
    assert status is HealthStatus.UNAVAILABLE
    assert "missing" in reason


async def test_vault_unreachable_check_present_dir(tmp_path: Path) -> None:
    check = make_vault_unreachable_check(tmp_path)
    status, _reason = await check.probe()
    assert status is HealthStatus.AVAILABLE


async def test_audit_chain_check_ok(tmp_path: Path) -> None:
    check = make_audit_chain_check(FakeAudit(ok=True))
    status, _reason = await check.probe()
    assert status is HealthStatus.AVAILABLE


async def test_audit_chain_check_broken(tmp_path: Path) -> None:
    check = make_audit_chain_check(FakeAudit(ok=False))
    status, reason = await check.probe()
    assert status is HealthStatus.UNAVAILABLE
    assert "broken" in reason


async def test_config_check_no_warnings() -> None:
    check = make_config_check(lambda: [])
    status, _reason = await check.probe()
    assert status is HealthStatus.AVAILABLE


async def test_config_check_with_warnings() -> None:
    check = make_config_check(lambda: ["bad profile layer"])
    status, reason = await check.probe()
    assert status is HealthStatus.LIMITED
    assert "1 config layer" in reason
