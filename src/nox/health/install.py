"""`install(core) -> HealthRuntime`: the four cross-cutting health checks and the degraded-mode
matrix
that reacts to them.

The checks are disk full, vault unreachable, audit chain broken and config invalid. Self-repair is
deliberately thin: the plugin manager already restarts crashed plugins and workers with its own
backoff and limit, and this module does not duplicate that. The one genuine repair action wired
here is disk-full, which runs the memory retention job early to free space when the memory
extension is installed. A broken audit chain has no code-level repair at all and escalates through
the kill switch's PIN-gated path instead of a silent, fake fix.
"""

from __future__ import annotations

from dataclasses import dataclass
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


class _MemoryRuntimeLike(Protocol):
    """What disk-full repair needs from the memory extension, if it is installed at all."""

    retention: Any


class _Core(Protocol):
    config: Any
    bus: Any
    state: Any
    security: Any
    health: Any
    extensions: dict[str, Any]


@dataclass(slots=True)
class HealthRuntime:
    """Handle for the caller; `stop` detaches the degraded-mode service from the bus."""

    degraded: DegradedModeService

    def stop(self) -> None:
        self.degraded.stop()


def install(core: _Core) -> HealthRuntime:
    cfg = core.config
    core.health.add_check(make_disk_full_check(cfg.paths.database_dir))
    core.health.add_check(make_vault_unreachable_check(cfg.paths.vault_dir))
    core.health.add_check(make_audit_chain_check(core.security.audit))
    core.health.add_check(make_config_check(lambda: cfg.warnings))

    async def _repair_disk_full() -> bool:
        """Free space by running memory retention early - only if memory is installed at all."""
        memory: _MemoryRuntimeLike | None = core.extensions.get("memory")
        if memory is None:
            return False
        report = await memory.retention.run()
        freed = int(report.memory_items_purged) + int(report.note_versions_purged)
        log.info("health.disk_full_repair", freed_rows=freed)
        return freed > 0

    async def _escalate_audit_chain(_component: str) -> None:
        await core.security.killswitch.engage(
            origin="audit", reason="audit hash chain verification failed"
        )

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
    return HealthRuntime(degraded=degraded)


__all__ = ["HealthRuntime", "install"]
