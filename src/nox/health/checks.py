"""Health checks for the Failure and Recovery Model rows `HealthService` did not yet cover: disk
full,
vault unreachable, audit chain broken, config invalid (Health & Recovery).

Each factory returns a `nox.core.health.Check` - a named async probe - so it plugs straight into
the existing `HealthService.add_check` without changing that module.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from nox.core.events import HealthStatus
from nox.core.health import Check
from nox.core.logging import get_logger

log = get_logger(__name__)

DISK_COMPONENT = "system.disk"
VAULT_COMPONENT = "memory.vault"
AUDIT_CHAIN_COMPONENT = "security.audit_chain"
CONFIG_COMPONENT = "system.config"

DEFAULT_DISK_WARN_MB = 1024.0
DEFAULT_DISK_CRITICAL_MB = 200.0


class ChainCheck(Protocol):
    """What the probe reads from a verification: `ChainVerification` has both."""

    ok: bool
    first_bad_seq: int | None


class AuditVerifier(Protocol):
    def verify_incremental(self) -> ChainCheck: ...


def make_disk_full_check(
    path: Path,
    *,
    warn_free_mb: float = DEFAULT_DISK_WARN_MB,
    critical_free_mb: float = DEFAULT_DISK_CRITICAL_MB,
    name: str = DISK_COMPONENT,
) -> Check:
    """Failure Model "Disk full": write errors stop memory writes/log rotation but keep the kill
    switch and pet - this probe is the early-warning side of that row."""

    async def probe() -> tuple[HealthStatus, str]:
        # Deliberately synchronous (ASYNC240): a stat()/small dir listing on local disk, same
        # trade-off as nox.memory.vault_index (module docstring there has the fuller rationale).
        probe_path = path if path.exists() else path.parent  # noqa: ASYNC240
        try:
            usage = shutil.disk_usage(probe_path)
        except OSError as exc:
            return HealthStatus.UNAVAILABLE, f"disk usage probe failed: {exc}"
        free_mb = usage.free / (1024 * 1024)
        if free_mb < critical_free_mb:
            return (
                HealthStatus.UNAVAILABLE,
                f"disk free {free_mb:.0f} MB < critical {critical_free_mb:.0f} MB",
            )
        if free_mb < warn_free_mb:
            return (
                HealthStatus.LIMITED,
                f"disk free {free_mb:.0f} MB < warning {warn_free_mb:.0f} MB",
            )
        return HealthStatus.AVAILABLE, f"disk free {free_mb:.0f} MB"

    return Check(name=name, probe=probe)


def make_vault_unreachable_check(vault_dir: Path, *, name: str = VAULT_COMPONENT) -> Check:
    """Failure Model "Vault unreachable (drive missing)": read-only memory from SQLite cache, no
    writes queued > 24 h - this probe is what the degraded-mode matrix reacts to."""

    async def probe() -> tuple[HealthStatus, str]:
        # Deliberately synchronous (ASYNC240): see the disk-full check above for the rationale.
        if not vault_dir.exists():  # noqa: ASYNC240
            return HealthStatus.UNAVAILABLE, f"vault_dir missing: {vault_dir}"
        if not vault_dir.is_dir():  # noqa: ASYNC240
            return HealthStatus.UNAVAILABLE, f"vault_dir is not a directory: {vault_dir}"
        try:
            next(vault_dir.iterdir(), None)  # noqa: ASYNC240
        except OSError as exc:
            return HealthStatus.UNAVAILABLE, f"vault_dir unreadable: {exc}"
        return HealthStatus.AVAILABLE, ""

    return Check(name=name, probe=probe)


def make_audit_chain_check(audit: AuditVerifier, *, name: str = AUDIT_CHAIN_COMPONENT) -> Check:
    """Failure Model "Audit chain broken": logged, kept, a new chain starts, and normal mode needs
    the PIN to resume - `DegradedModeService` is what actually engages the kill switch on this
    probe going UNAVAILABLE; this probe only reports honestly.

    Every round hashes only the rows appended since the previous one, in a worker thread: the full
    chain grows without bound and re-hashing it on the event loop every 30 s stalled everything
    else the loop does."""

    async def probe() -> tuple[HealthStatus, str]:
        try:
            result = await asyncio.to_thread(audit.verify_incremental)
        except Exception as exc:  # noqa: BLE001 - a broken verifier is itself UNAVAILABLE, not a crash
            return HealthStatus.UNAVAILABLE, f"audit chain verify error: {exc}"
        if result.ok:
            return HealthStatus.AVAILABLE, ""
        return HealthStatus.UNAVAILABLE, f"audit hash chain broken at entry {result.first_bad_seq}"

    return Check(name=name, probe=probe)


def make_config_check(
    warnings: Callable[[], list[object]], *, name: str = CONFIG_COMPONENT
) -> Check:
    """Failure Model "Config invalid": defaults + user notice - `warnings` is
    `NoxConfig.warnings` (populated by `load_config` when a layer was rejected)."""

    async def probe() -> tuple[HealthStatus, str]:
        found = warnings()
        if found:
            return (
                HealthStatus.LIMITED,
                f"{len(found)} configuration problem(s): rejected values use their default or, "
                "for a protective setting, its strictest value",
            )
        return HealthStatus.AVAILABLE, ""

    return Check(name=name, probe=probe)
