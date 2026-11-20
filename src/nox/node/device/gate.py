"""`NodeGate`: the node's own check before any handler runs - whatever the core decided.

A compromised core can send well-formed calls with valid receipts, so the node refuses on its own
authority. The checks run in this order (`CHECK_ORDER`, pinned by
`tests/unit/node/test_gate_order.py`); the first one that refuses gives the refusal the core
hears and the audit trail records:

1. `hard_prohibition` - the core's immutable list, by name and as a glob.
2. `game_lock` - mouse, keys, typing and window changes (`LOCKED_TOOLS`) are refused while a game
   or anti-cheat software runs on this device, a game is in front or owns the screen, or that
   cannot be told. The refusal is the lock's own reason (`input_lock`, installed by
   `nox.node.device.world`; without one: `game_lock_unavailable`). It answers before the stop and
   the receipt, so a call that could never run says so whatever else is wrong with it.
3. `stopped` - stopped on this device (only this device resumes), stopped by the core (until
   `nox/resume`), or after the dead-man rule fired (until the core is back and not stopped). The
   refusal is the stop's own reason.
4. `unknown_tool` - not a tool of this node (a side-effect tool that never declared how it
   treats paths is not registered at all).
5. `capability_disabled` - the tool's group is not enabled in this device's confirmed manifest,
   or the core narrowed it for this connection.
6. `risk_ceiling` - the tool's own risk is above `max_risk` of the manifest (default high;
   critical never).
7. `dialog_open` - while the person at the device is being asked, nothing else with a side effect
   runs: typing, clicking, a shell, a browser or a voice could all act on the dialog.
8. `receipt` - every call with a side effect carries the core's signature over exactly this call
   (`nox.node.wire.receipts`). Missing, for a lower risk than the tool's own, altered, expired or
   replayed -> refused (`receipt_missing`, `risk_understated`, `receipt_invalid`, ...). It comes
   before the arguments: nothing the core sent is looked at before it is known to be the core's.
9. `invalid_arguments` - the tool's strict model.
10. `node_home_protected` - no argument may name the node's own folder (compared as text).
11. `path_alias_refused` - no path argument in a spelling only the file system could resolve.
12. `precheck` - the tool's own text check, unless it already ran off the event loop; the refusal
    is the tool's own reason.
13. `risk_ceiling` - again, now for this call's own risk (a write that replaces a file is high).
14. `risk_understated` - the core must have judged this call at least as risky as the node does.
15. `budget` - side effects and high-risk calls per hour, counted here, independent of the core.

A call that passes may still need the **confirmation on this device**: high risk is decided by
the person at the device, not by the core (`GateDecision.needs_local_confirmation`, asked by
`nox.node.device.agent`).

Every decision - allowed or refused - goes into the node's own hash-chained audit trail
(`nox node log`), which the core cannot read or erase.

After the gate, each hand holds its own boundary when it acts (`nox.node.device.hands`): `file.*`
open every path through `nox.node.device.safe_fs` - inside a folder this device allowed, decided on
the real file at open time (on the handle), never the node's folder or the keyring's; `shell.run`
and `package.*` run only inside this device's `make_sandbox()`, which cannot see the node's folder,
and are refused (`sandbox_unavailable`) where there is none. A refusal there is audited like one of
the gate's. The whole path of a call is drawn in docs/ARCHITECTURE-NODES.md.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from nox.node.device.tools import NodeTool
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.node.wire.paths import is_remote_or_device_path, names_a_path_alias
from nox.node.wire.protocol import NoxCallMeta, safe_name
from nox.node.wire.receipts import ReceiptVerifier
from nox.security.model import AuditLog
from nox.security.prohibitions import matching_hard_prohibition

__all__ = [
    "CEILING",
    "CHECK_ORDER",
    "GateDecision",
    "INPUT_TOOLS",
    "LOCKED_TOOLS",
    "NodeGate",
    "RISK_ORDER",
]

RISK_ORDER = ("read", "low", "medium", "high", "critical")
#: What never runs on a node, whatever any manifest says.
CEILING = "high"
#: The budget's window.
BUDGET_WINDOW_S = 3600.0
#: The checks of `NodeGate._decide` in the order they run, named by the refusal they give. Four
#: of them pass on a reason from elsewhere and are named by their source: `game_lock` (the input
#: lock's reason), `stopped` (the stop state's), `receipt` (the receipt check's) and `precheck`
#: (the tool's own text check). `risk_ceiling` runs twice: for the tool, then for this call.
#: The module docstring lists the same order with the reasons;
#: `tests/unit/node/test_gate_order.py` holds this tuple against both the docstring and the code.
CHECK_ORDER: tuple[str, ...] = (
    "hard_prohibition",
    "game_lock",
    "stopped",
    "unknown_tool",
    "capability_disabled",
    "risk_ceiling",
    "dialog_open",
    "receipt",
    "invalid_arguments",
    "node_home_protected",
    "path_alias_refused",
    "precheck",
    "risk_ceiling",
    "risk_understated",
    "budget",
)
#: Tools that send input on this device: refused while a game runs or is in front - the first
#: thing the gate checks after the hard prohibitions. Ending a session sends nothing, so
#: `computer.session_end` always runs.
INPUT_TOOLS: frozenset[str] = frozenset(
    {"computer.session_start", "computer.pointer", "computer.keys", "desktop.type_text"}
)
#: Everything the game lock refuses: input, and every change to windows and programs (pulling a
#: game's window away or ending it is the same broken promise). Listing them stays allowed.
LOCKED_TOOLS: frozenset[str] = INPUT_TOOLS | frozenset(
    {
        "desktop.window_focus",
        "desktop.window_minimize",
        "desktop.window_restore",
        "desktop.window_close",
        "desktop.process_stop",
    }
)


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    #: Machine token of the check that refused (see `CHECK_ORDER`), or "" when allowed.
    reason: str = ""
    tool: NodeTool | None = None
    args: BaseModel | None = None
    #: High risk: the person at this device still has to say yes.
    needs_local_confirmation: bool = False
    #: The risk counted against the budget ("" for reads and refusals).
    charged: str = ""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


#: Argument fields that name a path: only these are refused for a path alias. Free text - a
#: note to write, a shell line such as `git log HEAD~1` - is not a path (the shell runs in the
#: sandbox; the node-folder check above still covers every string).
PATH_FIELDS = frozenset({"path", "to", "cwd", "folder", "dir", "directory", "source", "file"})


def _path_strings(value: Any, key: str = "") -> list[str]:
    if isinstance(value, str):
        return [value] if key in PATH_FIELDS else []
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in _path_strings(v, str(k))]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _path_strings(v, key)]
    return []


def _norm(text: str) -> str:
    return text.replace("\\", "/").rstrip("/").casefold()


def protected_forms(folder: Path) -> tuple[str, ...]:
    """The node folder as text, in every spelling the gate compares against - resolved ONCE, here,
    for the folder itself (never for anything the core sent)."""
    forms = {_norm(str(folder)), _norm(str(folder.absolute()))}
    try:
        forms.add(_norm(str(folder.resolve())))
    except OSError:
        pass
    return tuple(sorted(f for f in forms if f))


def touches(arguments: Any, forms: tuple[str, ...]) -> bool:
    """Whether any string in the arguments names the node folder, compared as **text only**:
    normalised separators, case folded. Nothing the core sent is ever resolved, expanded or
    opened here - `\\\\host\\share` stays a string, it never makes the device reach out (an
    SMB lookup could leak the user's NetNTLM hash) and never blocks the event loop.

    An early refusal (defence in depth), not the boundary: file tools on a node check the real
    file on its handle at the moment they open it (`nox.node.device.safe_fs`), and shell/package
    tools run only inside the node's `make_sandbox()`, where neither its folder nor the keyring is
    mounted or granted (`nox.node.device.hands`)."""
    for text in _strings(arguments):
        flat = _norm(text)
        if any(form in flat for form in forms):
            return True
    return False


def inside(path: str | Path, folder: Path) -> bool:
    """Whether the real path of `path` (symlinks, junctions, 8.3 names) lies in `folder`, resolved
    by name. Remote and device paths count as inside (refused) without being touched.

    A by-name answer can be outdated a moment later (a link swapped in between), so node file
    tools do not rely on it: they decide on the opened handle (`nox.node.device.safe_fs`)."""
    if is_remote_or_device_path(str(path)):
        return True
    try:
        resolved = _norm(str(Path(path).expanduser().resolve()))
        root = _norm(str(folder.resolve()))
    except (OSError, ValueError, RuntimeError):
        return True  # cannot tell: refuse
    return resolved == root or resolved.startswith(root + "/")


def _rank(risk: str) -> int:
    """A risk's rank; anything not understood ranks above critical (fail closed)."""
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
        #: Whether a confirmation dialog is open on this device right now (set by the agent).
        self.dialog_open: Callable[[], bool] = lambda: False
        #: Why no input may be sent on this device right now (a game), or None (`INPUT_TOOLS`).
        #: Until a check is installed, input is refused: the lock fails closed.
        self.input_lock: Callable[[], str | None] = lambda: "game_lock_unavailable"
        self._protected_forms = protected_forms(protected) if protected is not None else ()
        self.receipts = receipts
        self._clock = clock
        self._lock = threading.Lock()
        self._side_effects: deque[float] = deque()
        self._high: deque[float] = deque()
        # Registration rule: a tool with a side effect that has not declared how it treats
        # paths (`NodeTool.path_safe`) is not offered at all.
        self._tools = {n: t for n, t in tools.items() if t.risk == "read" or t.path_safe}
        self._manifest = manifest
        self._audit = audit
        #: The node's own folder (key, certificate, `node.yaml`, audit): no tool may touch it.
        self._protected = protected
        #: Groups the core narrowed for the current connection (`nox/policy`).
        self.narrowed: set[str] = set()

    @property
    def manifest(self) -> NodeManifest:
        return self._manifest

    def risk_of(self, name: str) -> str:
        """A tool's own risk; an unknown tool counts as a side effect."""
        tool = self._tools.get(name)
        return tool.risk if tool is not None else "critical"

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
        prechecked: bool = False,
    ) -> GateDecision:
        """The gate's decision, recorded. Fast and stateful: the slow, stateless text check runs
        before it (`precheck`, off the event loop) - `prechecked=True` says that happened.
        One call at a time (a lock), so the budget, the receipts' replay memory and the audit
        always see a consistent state, also from more than one thread."""
        with self._lock:
            decision = self._decide(name, arguments, meta, prechecked=prechecked)
            self._record(name, decision, call_id=call_id, by=by)
        return decision

    def precheck(self, name: str, arguments: Any) -> str | None:
        """A tool's text check (`NodeTool.precheck`) on its validated arguments: stateless - no
        lock, no budget, no receipt, no audit - so it may run in a worker thread with a time
        limit and simply be abandoned when the limit is hit. None when there is nothing to say
        (`check` decides everything else)."""
        tool = self._tools.get(name)
        if tool is None or tool.precheck is None or not isinstance(arguments, dict):
            return None
        try:
            args = tool.args.model_validate(arguments)
        except ValidationError:
            return None
        return tool.precheck(args)

    def _decide(
        self, name: str, arguments: Any, meta: NoxCallMeta | None, *, prechecked: bool = False
    ) -> GateDecision:
        if matching_hard_prohibition(name) is not None:
            return GateDecision(False, "hard_prohibition")
        if name in LOCKED_TOOLS:
            try:
                locked = self.input_lock()
            except Exception:  # noqa: BLE001 - a check that fails is a lock, never a pass
                locked = "game_lock_unavailable"
            if locked is not None:
                return GateDecision(False, locked)
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
        if self.dialog_open() and tool.risk != "read":
            # While the person is being asked, nothing else with a side effect runs - typing,
            # clicking, a shell, a browser or a voice could all act on the dialog.
            return GateDecision(False, "dialog_open", tool)
        if tool.risk != "read":
            # The core's signature first: nothing the core sent is looked at before it is known
            # to be the core's, for exactly this call.
            refusal = self._check_receipt(tool, arguments, meta)
            if refusal is not None:
                return GateDecision(False, refusal, tool)
        try:
            args = tool.args.model_validate(arguments if isinstance(arguments, dict) else {})
        except ValidationError:
            return GateDecision(False, "invalid_arguments", tool)
        if not isinstance(arguments, dict):
            return GateDecision(False, "invalid_arguments", tool)
        if self._protected_forms and touches(args.model_dump(), self._protected_forms):
            return GateDecision(False, "node_home_protected", tool)
        if any(names_a_path_alias(text) for text in _path_strings(args.model_dump())):
            return GateDecision(False, "path_alias_refused", tool)
        if tool.precheck is not None and not prechecked:
            early = tool.precheck(args)
            if early is not None:
                return GateDecision(False, early, tool)
        # This call's own risk (a write that replaces a file is high): the core must have judged
        # it at least as risky, and the ceiling applies to it as well.
        risk = tool.call_risk(args)
        if not self._within(risk):
            return GateDecision(False, "risk_ceiling", tool)
        if risk != "read" and (meta is None or _rank(meta.risk) < _rank(risk)):
            return GateDecision(False, "risk_understated", tool)
        charged = ""
        if risk != "read":
            # By rank, never by spelling: the higher of the node's and the core's risk, an
            # unknown value as the highest, clamped to the ceiling's name for the books.
            rank = max(_rank(risk), _rank(meta.risk) if meta is not None else 0)
            charged = RISK_ORDER[min(rank, _rank(CEILING))]
            if not self._spend(charged):
                return GateDecision(False, "budget", tool)
        high = _rank("high")
        local = _rank(risk) >= high or (
            meta is not None and (_rank(meta.risk) >= high or meta.confirm_on_device)
        )
        charged_risk = "" if risk == "read" else charged
        return GateDecision(
            True, "", tool, args, needs_local_confirmation=local, charged=charged_risk
        )

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
            tainted=meta.tainted,
            issued_at=meta.issued_at,
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
        is_high = _rank(risk) >= _rank("high")
        if is_high and len(self._high) >= self._manifest.max_high_per_hour:
            return False
        self._side_effects.append(now)
        if is_high:
            self._high.append(now)
        return True

    def restore_budget(self, audit: Any) -> None:
        """After a restart the hour's spending is read back from the node's own audit trail, so a
        restart does not refill the budget."""
        conn = getattr(audit, "_conn", None)
        if conn is None:
            return
        since = datetime.now(UTC) - timedelta(seconds=BUDGET_WINDOW_S)
        try:
            rows = conn.execute(
                "SELECT ts, details_json FROM audit_log WHERE action = 'node.call'"
                " AND decision = 'allow' AND ts > ? ORDER BY seq",
                (since.isoformat(),),
            ).fetchall()
        except Exception:  # noqa: BLE001 - no history means no restore, never a crash
            return
        now = self._clock()
        for ts, details in rows:
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(str(ts))).total_seconds()
                charged = json.loads(details or "{}").get("charged", "")
            except (ValueError, TypeError):
                continue
            if not charged or charged == "read":
                continue
            # Anything at or above high - or not understood - counts as high.
            self._side_effects.append(now - age)
            if _rank(str(charged)) >= _rank("high"):
                self._high.append(now - age)

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
            details={"charged": decision.charged} if decision.charged else None,
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
