"""`NodeGate`: the node's own check before any handler runs - whatever the core decided.

A compromised core can send well-formed calls with valid receipts; the node refuses on its own
authority (spec C §1.6). In order:

1. **Hard prohibitions** - the core's immutable list, by name and as a glob.
2. **Stopped** - stopped on this device (only this device resumes), stopped by the core (until
   `nox/resume`), or after the dead-man rule fired (until the core is back and not stopped).
3. **Known tool, capability** - the tool's group enabled in this device's confirmed manifest and
   not narrowed by the core for this connection.
4. **Risk ceiling** - `max_risk` of the manifest (default high; critical never); the core may not
   claim a lower risk than the node's own for the tool.
5. **Arguments** - the tool's strict model; no argument may name the node's own folder.
6. **Receipt** - every call with a side effect carries the core's signature over exactly this
   call (`nox.node.receipts`); missing, altered, expired or replayed -> refused.
7. **Budget** - side effects and high-risk calls per hour, counted here, independent of the core.
8. High risk then still needs the **confirmation on this device** (`nox.node.agent`, D1).

Every decision - allowed or refused - goes into the node's own hash-chained audit trail
(`nox node log`), which the core cannot read or erase. Shell and package tools on nodes run only
in the node's own sandbox (C5).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from nox.node.manifest import GROUPS, NodeManifest
from nox.node.protocol import NoxCallMeta, safe_name
from nox.node.receipts import ReceiptVerifier
from nox.node.tools import NodeTool
from nox.security.model import AuditLog
from nox.security.prohibitions import matching_hard_prohibition

__all__ = ["CEILING", "GateDecision", "NodeGate", "RISK_ORDER"]

RISK_ORDER = ("read", "low", "medium", "high", "critical")
#: What never runs on a node, whatever any manifest says.
CEILING = "high"
#: The budget's window.
BUDGET_WINDOW_S = 3600.0


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    #: Machine token: hard_prohibition | unknown_tool | capability_disabled | risk_ceiling |
    #: invalid_arguments | "" when allowed.
    reason: str = ""
    tool: NodeTool | None = None
    args: BaseModel | None = None
    #: High risk: the person at this device still has to say yes.
    needs_local_confirmation: bool = False


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
        stopped: Callable[[], str | None] = lambda: None,
        receipts: ReceiptVerifier | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: Why the node refuses everything right now (set by the agent), or None.
        self.stop_state = stopped
        self.receipts = receipts
        self._clock = clock
        self._side_effects: deque[float] = deque()
        self._high: deque[float] = deque()
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
        self,
        name: str,
        arguments: Any,
        *,
        call_id: str = "",
        by: str = "core",
        meta: NoxCallMeta | None = None,
    ) -> GateDecision:
        decision = self._decide(name, arguments, meta)
        self._record(name, decision, call_id=call_id, by=by)
        return decision

    def _decide(self, name: str, arguments: Any, meta: NoxCallMeta | None) -> GateDecision:
        if matching_hard_prohibition(name) is not None:
            return GateDecision(False, "hard_prohibition")
        stopped = self.stop_state()
        if stopped is not None:
            return GateDecision(False, stopped)
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
        if tool.risk != "read":
            refusal = self._check_receipt(tool, arguments, meta)
            if refusal is not None:
                return GateDecision(False, refusal, tool)
            if not self._spend(tool.risk):
                return GateDecision(False, "budget", tool)
        local = tool.risk == "high" or (
            meta is not None and (meta.risk == "high" or meta.confirm_on_device)
        )
        return GateDecision(True, "", tool, args, needs_local_confirmation=local)

    def _check_receipt(
        self, tool: NodeTool, arguments: Any, meta: NoxCallMeta | None
    ) -> str | None:
        if meta is None or self.receipts is None:
            return "receipt_missing"
        if _rank(meta.risk) < _rank(tool.risk):
            return "risk_understated"  # the core may not claim the call is less risky
        return self.receipts.check(
            receipt=meta.receipt,
            call_id=meta.call_id,
            tool=tool.name,
            arguments=arguments,
            risk=meta.risk,
            decided_by=meta.decided_by,
            confirm_on_device=meta.confirm_on_device,
            deadline=meta.deadline,
        )

    def _spend(self, risk: str) -> bool:
        """Count one side effect (and one high-risk call); False when the hour is used up."""
        now = self._clock()
        for window in (self._side_effects, self._high):
            while window and window[0] <= now - BUDGET_WINDOW_S:
                window.popleft()
        if len(self._side_effects) >= self._manifest.max_calls_per_hour:
            return False
        if risk == "high" and len(self._high) >= self._manifest.max_high_per_hour:
            return False
        self._side_effects.append(now)
        if risk == "high":
            self._high.append(now)
        return True

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
