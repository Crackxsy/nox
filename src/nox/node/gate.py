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
(`nox node log`), which the core cannot read or erase.

After the gate, each hand holds its own boundary when it acts (`nox.node.hands`): `file.*` open
every path through `nox.node.safe_fs` - inside a folder this device allowed, decided on the real
file at open time (on the handle), never the node's folder or the keyring's; `shell.run` and
`package.*` run only inside this device's `make_sandbox()`, which cannot see the node's folder,
and are refused (`sandbox_unavailable`) where there is none. A refusal there is audited like one
of the gate's.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    file on its handle at the moment they open it (`nox.node.safe_fs`), and shell/package tools
    run only inside the node's `make_sandbox()`, where neither its folder nor the keyring is
    mounted or granted (`nox.node.hands`)."""
    for text in _strings(arguments):
        flat = _norm(text)
        if any(form in flat for form in forms):
            return True
    return False


#: An 8.3 short name (`NOXNOD~1`): another spelling of a folder that text cannot compare.
_SHORT_NAME = re.compile(r"(^|[\\/])[^\\/\s]{1,6}~\d")


def names_a_path_alias(text: str) -> bool:
    """Whether `text` holds a path spelling that only the file system could resolve - `..`
    segments, `~` at the start of a path, 8.3 short names, UNC or device paths. Decided on the
    text alone: the gate never asks the file system about a core string."""
    for word in text.replace('"', " ").replace("'", " ").split():
        if is_remote_or_device_path(word):
            return True
        if word == "~" or word.startswith(("~/", "~\\")):
            return True
        segments = re.split(r"[\\/]", word)
        if ".." in segments and len(segments) > 1:
            return True
        if _SHORT_NAME.search(word):
            return True
    return False


def is_remote_or_device_path(path: str) -> bool:
    """UNC (`\\\\host\\share`, `//host/share`) and Win32 device paths (`\\\\?\\`,
    `\\\\.\\`): never opened or resolved on behalf of the core."""
    text = path.strip()
    return text.startswith(("\\\\", "//")) or text.replace("/", "\\").startswith(
        ("\\\\?\\", "\\\\.\\")
    )


def inside(path: str | Path, folder: Path) -> bool:
    """Whether the real path of `path` (symlinks, junctions, 8.3 names) lies in `folder`, resolved
    by name. Remote and device paths count as inside (refused) without being touched.

    A by-name answer can be outdated a moment later (a link swapped in between), so node file
    tools do not rely on it: they decide on the opened handle (`nox.node.safe_fs`)."""
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
        self._protected_forms = protected_forms(protected) if protected is not None else ()
        self.receipts = receipts
        self._clock = clock
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
        if self.dialog_open() and tool.risk != "read":
            # While the person is being asked, nothing else with a side effect runs - typing,
            # clicking, a shell, a browser or a voice could all act on the dialog (review M-c).
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
        restart does not refill the budget (review N1)."""
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
