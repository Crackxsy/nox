"""`NodeGate`: the node's own check before any handler runs - whatever the core decided.

A compromised core can send well-formed calls; the node refuses on its own authority (spec C
§1.6). In this release, in order:

1. **Hard prohibitions** - the same immutable list as the core (`nox.security.hardlist`), by
   name and as a glob. Nothing configures them away.
2. **Known tool** - only what this node implements exists.
3. **Capability** - the tool's group must be enabled in this device's `node.yaml` and not
   narrowed by the core for this connection.
4. **Risk ceiling** - at most `CEILING` until the signed core receipts and the local confirmation
   for high risk exist on the node (C4); `node.yaml`'s `max_risk` can lower it, never raise it.
5. **Arguments** - validated against the tool's strict model.

Every decision - allowed or refused - goes into the node's own hash-chained audit trail
(`nox node log`), which the core cannot read or erase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from nox.node.manifest import NodeManifest
from nox.node.tools import NodeTool
from nox.security.model import AuditLog
from nox.security.prohibitions import matching_hard_prohibition

__all__ = ["CEILING", "GateDecision", "NodeGate", "RISK_ORDER"]

RISK_ORDER = ("read", "low", "medium", "high", "critical")
#: The highest risk a node runs before C4 adds receipts and the local confirmation dialog.
CEILING = "low"


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    #: Machine token: hard_prohibition | unknown_tool | capability_disabled | risk_ceiling |
    #: invalid_arguments | "" when allowed.
    reason: str = ""
    tool: NodeTool | None = None
    args: BaseModel | None = None


def _rank(risk: str) -> int:
    return RISK_ORDER.index(risk) if risk in RISK_ORDER else len(RISK_ORDER)


class NodeGate:
    def __init__(
        self,
        tools: dict[str, NodeTool],
        manifest: NodeManifest,
        audit: AuditLog | None,
    ) -> None:
        self._tools = tools
        self._manifest = manifest
        self._audit = audit
        #: Groups the core narrowed for the current connection (`nox/policy`).
        self.narrowed: set[str] = set()

    @property
    def manifest(self) -> NodeManifest:
        return self._manifest

    def offered(self) -> list[NodeTool]:
        """The tools this node lists to the core right now."""
        groups = self._manifest.effective(self.narrowed)
        return [t for t in self._tools.values() if t.group in groups and self._within(t.risk)]

    def _within(self, risk: str) -> bool:
        ceiling = min(_rank(CEILING), _rank(self._manifest.max_risk))
        return _rank(risk) <= ceiling

    def check(
        self, name: str, arguments: Any, *, call_id: str = "", by: str = "core"
    ) -> GateDecision:
        decision = self._decide(name, arguments)
        self._record(name, decision, call_id=call_id, by=by)
        return decision

    def _decide(self, name: str, arguments: Any) -> GateDecision:
        if matching_hard_prohibition(name) is not None:
            return GateDecision(False, "hard_prohibition")
        tool = self._tools.get(name)
        if tool is None:
            return GateDecision(False, "unknown_tool")
        if tool.group not in self._manifest.effective(self.narrowed):
            return GateDecision(False, "capability_disabled", tool)
        if not self._within(tool.risk):
            return GateDecision(False, "risk_ceiling", tool)
        try:
            args = tool.args.model_validate(arguments if isinstance(arguments, dict) else {})
        except ValidationError:
            return GateDecision(False, "invalid_arguments", tool)
        if not isinstance(arguments, dict):
            return GateDecision(False, "invalid_arguments", tool)
        return GateDecision(True, "", tool, args)

    def _record(self, name: str, decision: GateDecision, *, call_id: str, by: str) -> None:
        if self._audit is None:
            return
        self._audit.append(
            actor=by,
            tool=name[:96],
            action="node.call",
            target=call_id[:64],
            decision="allow" if decision.allowed else "deny",
            result=decision.reason or "ok",
        )

    def narrow(self, groups: list[str]) -> None:
        """The core's `nox/policy`: take groups away for this connection. It cannot add any."""
        self.narrowed = {g for g in groups if isinstance(g, str)}
        if self._audit is not None:
            self._audit.append(
                actor="core",
                tool="node",
                action="node.policy",
                target="",
                decision="allow",
                result="narrowed:" + ",".join(sorted(self.narrowed))[:200],
            )
