"""The core's record of what a plugin's own egress guard decided.

A plugin worker checks every connection attempt against its manifest-scoped `EgressGuard`
(`nox.plugins.api.PluginEgressGuard`) and reports each decision to the core as
`plugin.egress.report`. This module writes those reports to the audit log with the plugin as the
actor, checks them against the manifest - an "allowed" connection to an endpoint the manifest
never declared is recorded as `undeclared`, because the plugin's guard should have refused it -
and rate-limits them per plugin, so a chatty or hostile plugin cannot flood the audit chain.

Reporting is cooperative: plugin code that opens a socket without asking its guard reports
nothing. `docs/SECURITY.md` says so; this module does not pretend otherwise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.ipc.errors import ERR_INTERNAL, ERR_RATE_LIMITED, IpcError
from nox.plugins.manifest import ManifestError, PluginManifest, split_endpoint
from nox.security.egress import entry_matches, is_loopback, loopback_entry_matches

log = get_logger(__name__)

#: Egress reports one plugin may send per minute before further ones are dropped (and counted).
EGRESS_REPORTS_PER_MIN = 120
_WINDOW_S = 60.0


class EgressReport(BaseModel):
    """One decision of the plugin-side egress guard (`plugin.egress.report` payload)."""

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=0, le=65535)
    scheme: str = Field(default="", max_length=16)
    allowed: bool
    rule_id: str = Field(default="", max_length=128)


class AuditAppend(Protocol):
    def append(
        self,
        *,
        actor: str,
        tool: str,
        action: str,
        target: str,
        decision: str,
        result: str,
        task_id: str | None = None,
        details: dict[str, str] | None = None,
    ) -> Any: ...


@dataclass
class _Window:
    arrivals: list[float] = field(default_factory=list)
    dropped: int = 0


def declares_endpoint(manifest: PluginManifest, host: str, port: int) -> bool:
    """Whether `host:port` is one of the manifest's own `network.egress` entries."""
    host = host.strip().lower().rstrip(".")
    match = loopback_entry_matches if is_loopback(host) else entry_matches
    for entry in manifest.network.egress:
        try:
            split_endpoint(entry)
        except ManifestError:
            continue
        if match(entry, host, port):
            return True
    return False


class EgressReportAuditor:
    """Audits plugin egress reports; see the module docstring."""

    def __init__(
        self,
        audit: AuditAppend | None,
        *,
        clock: Callable[[], float],
        per_minute: int = EGRESS_REPORTS_PER_MIN,
    ) -> None:
        self._audit = audit
        self._clock = clock
        self._per_minute = per_minute
        self._windows: dict[str, _Window] = {}

    def record(self, plugin_id: str, manifest: PluginManifest, report: EgressReport) -> None:
        """Audit one report. Raises `IpcError` when rate-limited or when it cannot be written."""
        if not self._admit(plugin_id):
            raise IpcError(ERR_RATE_LIMITED, "too many egress reports", retryable=True)
        declared = declares_endpoint(manifest, report.host, report.port)
        result = "ok" if report.allowed else "denied"
        if report.allowed and not declared:
            result = "undeclared"
            log.error(
                "plugin.egress_undeclared", plugin=plugin_id, host=report.host, port=report.port
            )
        if self._audit is None:
            return
        try:
            self._audit.append(
                actor=f"plugin:{plugin_id}",
                tool="network",
                action="plugin.egress",
                target=f"{report.scheme or 'tcp'}://{report.host}:{report.port}",
                decision="allow" if report.allowed else "deny",
                result=result,
                details={"rule_id": report.rule_id, "declared": "yes" if declared else "no"},
            )
        except Exception as exc:  # noqa: BLE001 - reported to the plugin as an internal error
            log.error("plugin.egress_audit_failed", error=f"{type(exc).__name__}: {exc}")
            raise IpcError(ERR_INTERNAL, "egress report could not be audited") from exc

    def _admit(self, plugin_id: str) -> bool:
        now = self._clock()
        window = self._windows.setdefault(plugin_id, _Window())
        window.arrivals = [t for t in window.arrivals if now - t < _WINDOW_S]
        if len(window.arrivals) >= self._per_minute:
            window.dropped += 1
            if window.dropped == 1:
                log.warning("plugin.egress_reports_dropped", plugin=plugin_id)
            return False
        if window.dropped:
            log.warning("plugin.egress_reports_resumed", plugin=plugin_id, dropped=window.dropped)
            window.dropped = 0
        window.arrivals.append(now)
        return True
