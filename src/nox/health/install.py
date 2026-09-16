"""`install(core) -> None`: wires the EPIC-09 Health & Recovery pieces into an already-booted
`NoxCore` without touching `src/nox/app.py` - the four missing Failure and Recovery Model checks
(disk full, vault unreachable, audit chain broken, config invalid) onto the existing
`HealthService`, plus the degraded-mode matrix that reacts to them.

Self-repair: `PluginManager` already restarts crashed plugins/workers with its own backoff and
limit (`PluginManagerSettings.restart_limit`/`restart_window_s`) - this module does not duplicate
that. The one genuine repair action wired here is disk-full -> run the memory retention job early
(when `nox.memory` is installed) to free space; audit-chain-broken has no code-level repair and
instead escalates through the kill switch's existing PIN-gated security path (Failure and Recovery
Model "require PIN to resume normal mode"), never a fake silent fix.
"""

from __future__ import annotations

from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.health.checks import (
    make_audit_chain_check,
    make_config_check,
    make_disk_full_check,
    make_vault_unreachable_check,
)
from nox.health.degraded import DegradedModeService, SelfRepair, SelfRepairPolicy

log = get_logger(__name__)


class _Core(Protocol):
    config: Any
    bus: Any
    state: Any
    security: Any
    health: Any


def install(core: _Core) -> None:
    cfg = core.config
    core.health.add_check(make_disk_full_check(cfg.paths.database_dir))
    core.health.add_check(make_vault_unreachable_check(cfg.paths.vault_dir))
    core.health.add_check(make_audit_chain_check(core.security.audit))
    core.health.add_check(make_config_check(lambda: cfg.warnings))

    async def _repair_disk_full() -> bool:
        memory = getattr(core, "memory", None)
        if memory is None:
            return False
        report = await memory.retention.run()
        freed = int(report.memory_items_purged) + int(report.note_versions_purged)
        log.info("health.disk_full_repair", freed_rows=freed)
        return freed > 0

    async def _escalate_audit_chain(component: str) -> None:
        killswitch = getattr(core.security, "killswitch", None)
        if killswitch is None:
            log.error("health.audit_chain_escalation_unavailable", component=component)
            return
        await killswitch.engage(origin="audit", reason="audit hash chain verification failed")

    repair = SelfRepair(
        actions={"system.disk": _repair_disk_full},
        policy=SelfRepairPolicy(max_attempts=3, window_s=3600.0),
    )
    degraded = DegradedModeService(
        core.bus,
        core.state,
        repair=repair,
        escalate={"security.audit_chain": _escalate_audit_chain},
    )
    degraded.start()

    # Not part of the `_Core` Protocol (kept narrow for typing); a plain attribute for tests and
    # any later service that needs direct access, e.g. a dashboard health-view reading it.
    core.degraded = degraded  # type: ignore[attr-defined]
