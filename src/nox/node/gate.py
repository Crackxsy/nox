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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from nox.node.manifest import GROUPS, NodeManifest
from nox.node.protocol import safe_name
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


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _norm(text: str) -> str:
    return text.replace("\\", "/").rstrip("/").casefold()


def touches(arguments: Any, folder: Path) -> bool:
    """Whether any string in the arguments names `folder` or something inside it - as a path,
    a word of a command line, or anywhere in the text. Errs on refusing.

    An early refusal (defence in depth), not the boundary: the boundary is where access happens.
    Paths are compared after `resolve()` (symlinks, junctions, 8.3 names, case); file tools on a
    node must check the resolved real path themselves, and shell/package tools run only in the
    node's sandbox without access to its folder or the keyring.
    TODO(C5): node file tools check `inside()` on the resolved path at open time; shell/package
    tools on nodes only inside `make_sandbox()` with the node folder not mounted."""
    try:
        root = _norm(str(folder.resolve()))
    except OSError:
        root = _norm(str(folder))
    plain = _norm(str(folder))
    for text in _strings(arguments):
        flat = _norm(text)
        if root in flat or plain in flat:
            return True
        for word in text.replace('"', " ").replace("'", " ").split():
            try:
                resolved = _norm(str(Path(word).expanduser().resolve()))
            except (OSError, ValueError, RuntimeError):
                continue
            if resolved == root or resolved.startswith(root + "/"):
                return True
    return False


def inside(path: str | Path, folder: Path) -> bool:
    """Whether `path`, resolved (symlinks, junctions, 8.3 names), lies in `folder` (resolved)."""
    try:
        resolved = _norm(str(Path(path).expanduser().resolve()))
        root = _norm(str(folder.resolve()))
    except (OSError, ValueError, RuntimeError):
        return True  # cannot tell: refuse
    return resolved == root or resolved.startswith(root + "/")


def _rank(risk: str) -> int:
    return RISK_ORDER.index(risk) if risk in RISK_ORDER else len(RISK_ORDER)


class NodeGate:
    def __init__(
        self,
        tools: dict[str, NodeTool],
        manifest: NodeManifest,
        audit: AuditLog | None,
        *,
        protected: Path | None = None,
    ) -> None:
        self._tools = tools
        self._manifest = manifest
        self._audit = audit
        #: The node's own folder (key, certificate, `node.yaml`, audit): no tool may touch it.
        self._protected = protected
        #: Groups the core narrowed for the current connection (`nox/policy`).
        self.narrowed: set[str] = set()

    @property
    def manifest(self) -> NodeManifest:
        return self._manifest

    def set_manifest(self, manifest: NodeManifest) -> None:
        """The device's manifest changed (`nox node deny` takes effect without a restart)."""
        self._manifest = manifest

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
        if self._protected is not None and touches(args.model_dump(), self._protected):
            return GateDecision(False, "node_home_protected", tool)
        return GateDecision(True, "", tool, args)

    def _record(self, name: str, decision: GateDecision, *, call_id: str, by: str) -> None:
        if self._audit is None:
            return
        self._audit.append(
            actor=by,
            tool=safe_name(name),
            action="node.call",
            target=safe_name(call_id, 64),
            decision="allow" if decision.allowed else "deny",
            result=decision.reason or "ok",
        )

    def narrow(self, groups: list[str]) -> None:
        """The core's `nox/policy`: take groups away for this connection. It cannot add any."""
        self.narrowed = {g for g in groups if isinstance(g, str) and g in GROUPS}
        if self._audit is not None:
            self._audit.append(
                actor="core",
                tool="node",
                action="node.policy",
                target="",
                decision="allow",
                result="narrowed:" + ",".join(sorted(self.narrowed)),
            )
