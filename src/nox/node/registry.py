"""`NodeRegistry`: which nodes are online on the core, what each one offers, and the one call path.

Fed by the node listener (`on_connected`, `on_disconnected`, `on_tools_changed`). A node's tool
list is the node's word, so it is filtered before anything else sees it: only names Nox itself
routes (`ROUTABLE`), never a hard-prohibited name, never a name with the wire separator, at most
`MAX_TOOLS`. The risk a node reports can only raise the core's view of a tool, never lower it.

Results from a node are untrusted data: they are schema-checked here (`clean_result`), bounded,
and the tool loop marks the conversation tainted like any other untrusted result.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nox.core.logging import get_logger
from nox.mcp.client import McpClientError
from nox.node.confirm import CONFIRM_TIMEOUT_S
from nox.node.listener import NodeSession
from nox.node.names import display_name, label
from nox.node.pairing import NodePairing
from nox.node.policy import NodePolicy, group_of, reported_groups, send_policy
from nox.node.receipts import MAX_LIFETIME_S, ReceiptSigner
from nox.node.repo import NodeRepository
from nox.security.prohibitions import matching_hard_prohibition
from nox.tools.executor import CALL_DECISION

log = get_logger(__name__)

__all__ = [
    "MAX_RESULT_CHARS",
    "NodeEntry",
    "NodeRegistry",
    "ROUTABLE",
    "clean_result",
    "display_name",
]

#: Tools the core routes to nodes in this release. A node offering anything else is ignored.
ROUTABLE: frozenset[str] = frozenset({"system.info", "notify.show"})
MAX_TOOLS = 64
#: What a node's result may put in front of the model, at most.
MAX_RESULT_CHARS = 16_000
RISKS = ("read", "low", "medium", "high", "critical")
#: An incompatible (outdated) node offers only this (spec C §1.8).
OUTDATED_TOOLS: frozenset[str] = frozenset({"system.info"})


#: How long a call waits for a device that has just connected to finish announcing its tools.
LISTING_WAIT_S = 3.0


def _set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@dataclass
class NodeEntry:
    device_id: str
    name: str
    online: bool = False
    compatible: bool = True
    #: tool name -> the risk the node reports for it
    tools: dict[str, str] = field(default_factory=dict)
    last_seen: datetime | None = None
    linked_device_id: str | None = None
    session: NodeSession | None = None
    #: The node can ask the person at the device (it said so at `initialize`).
    confirmer: bool = False
    #: Groups the owner switched off for this node in the dashboard (`nox.node.policy`).
    narrowed: frozenset[str] = frozenset()
    #: Narrowings asked for and not yet finished, counted per group: each request adds one and
    #: takes away only its own, so the group is off while any of them is still on its way.
    pending_narrow: Counter[str] = field(default_factory=Counter)
    #: Cleared while a new connection's first tool list is being recorded: until then nothing is
    #: offered from it, and a call waits for it (`settle`) instead of failing as offline.
    listed: asyncio.Event = field(default_factory=_set_event)

    @property
    def label(self) -> str:
        """What the model sees: the plain name and a short id the device cannot choose."""
        return label(self.name, self.device_id)

    def narrowed_out(self, tool: str) -> bool:
        """Whether the tool's group is switched off here (not on the device)."""
        group = group_of(tool)
        return group in self.narrowed or (group is not None and self.pending_narrow[group] > 0)

    @property
    def connecting(self) -> bool:
        """Connected, its first tool list not recorded yet."""
        return self.online and not self.listed.is_set()

    def offers(self, tool: str) -> bool:
        if not self.online or self.connecting or tool not in self.tools:
            return False
        # Whatever `tools` holds: never a tool of no known group or of a narrowed one.
        if group_of(tool) is None or self.narrowed_out(tool):
            return False
        if self.session is not None and not self.session.alive:
            return False  # no ping answered for a while: as good as gone
        return self.compatible or tool in OUTDATED_TOOLS


def clean_result(raw: Any, *, device: str) -> dict[str, Any]:
    """A node's `tools/call` answer, as data the model may see - or a plain error. Never the raw
    answer: a node is not trusted to shape what reaches the model."""
    if not isinstance(raw, dict):
        return {"error": "malformed_result", "device": device}
    texts: list[str] = []
    content = raw.get("content")
    if isinstance(content, list):
        texts = [
            str(item["text"])
            for item in content[:16]
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
    elif content is not None:
        return {"error": "malformed_result", "device": device}
    if raw.get("isError") is True:
        nox = raw.get("_nox")
        refused = nox.get("refused") if isinstance(nox, dict) else None
        if isinstance(refused, str):
            return {"error": "refused_on_device", "reason": refused[:64], "device": device}
        return {"error": "failed_on_device", "detail": " ".join(texts)[:500], "device": device}
    out: dict[str, Any] = {"device": device}
    structured = raw.get("structuredContent")
    if isinstance(structured, dict):
        encoded = json.dumps(structured, ensure_ascii=False)
        if len(encoded) <= MAX_RESULT_CHARS:
            out["result"] = structured
            return out
        texts = [encoded]
    elif structured is not None:
        return {"error": "malformed_result", "device": device}
    text = "\n".join(texts)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " [... truncated by Nox]"
    out["text"] = text
    return out


class NodeRegistry:
    def __init__(
        self,
        pairing: NodePairing,
        nodes: NodeRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        signer: ReceiptSigner | None = None,
        core_id: Callable[[], str] = lambda: "",
    ) -> None:
        self._signer = signer
        self._core_id = core_id
        self._pairing = pairing
        self._nodes = nodes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entries: dict[str, NodeEntry] = {}
        #: What the owner narrowed per node on the core (`nox.node.policy`).
        self.policy = NodePolicy(nodes, clock=self._clock)
        #: One per node: a listing and a narrowing never interleave (read -> compute -> assign ->
        #: tell the node is one step).
        self._locks: dict[str, asyncio.Lock] = {}
        #: Narrow / clear requests in arrival order; per (node, group) the last one applied.
        self._seq = itertools.count(1)
        self._applied: dict[tuple[str, str], int] = {}
        #: Narrowings being applied (kept referenced until done; see `set_narrowed`).
        self._applying: set[asyncio.Future[None]] = set()
        #: Nodes with a listing running for `list_changed`, and those that changed again meanwhile.
        self._refreshing: set[str] = set()
        self._dirty: set[str] = set()

    def _lock(self, device_id: str) -> asyncio.Lock:
        lock = self._locks.get(device_id)
        if lock is None:
            lock = self._locks[device_id] = asyncio.Lock()
        return lock

    # -- listener hooks ----------------------------------------------------------------------------

    async def on_connected(self, session: NodeSession) -> None:
        entry = self._entry(session.device_id, session.device.name)
        # Online, but offering nothing until its own first list is recorded: never the old list
        # of a previous connection. A call meanwhile waits for it (`settle`, `call`).
        listed = asyncio.Event()
        entry.listed = listed
        entry.session, entry.online, entry.compatible = session, True, session.compatible
        entry.confirmer = session.hello.confirmer
        entry.last_seen = self._clock()
        try:
            # The node forgot the core's narrowing with the old connection: a complete listing.
            await self.refresh(session, complete=True)
        finally:
            listed.set()

    async def settle(self, timeout_s: float = LISTING_WAIT_S) -> None:
        """Wait (at most `timeout_s`) until every device that has just connected has its tool
        list recorded - so a request right after a device comes online finds it."""
        waiting = [e.listed.wait() for e in self._entries.values() if e.connecting]
        if not waiting:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*waiting), timeout_s)

    async def on_disconnected(self, session: NodeSession) -> None:
        entry = self._entries.get(session.device_id)
        if entry is not None and entry.session is session:
            entry.online, entry.session = False, None
            entry.last_seen = self._clock()

    async def on_tools_changed(self, session: NodeSession) -> None:
        """Coalesced: while a listing for this node runs, any number of changes queue exactly
        one more listing - a flood of `list_changed` cannot pile up requests."""
        device_id = session.device_id
        if device_id in self._refreshing:
            self._dirty.add(device_id)
            return
        self._refreshing.add(device_id)
        try:
            while True:
                self._dirty.discard(device_id)
                await self.refresh(session)
                if device_id not in self._dirty:
                    return
        finally:
            self._refreshing.discard(device_id)
            self._dirty.discard(device_id)

    async def refresh(self, session: NodeSession, *, complete: bool = False) -> None:
        async with self._lock(session.device_id):
            await self._refresh(session, complete=complete)

    async def _refresh(self, session: NodeSession, *, complete: bool) -> None:
        """Under the node's lock. Threads only read and write the database and return values;
        every assignment to the entry happens here, on the loop."""
        entry = self._entries.get(session.device_id)
        if entry is None or entry.session is not session:
            return
        try:
            listed = await session.client.request("tools/list", {}, timeout_s=5)
        except McpClientError as exc:
            log.warning("node.tools_list_failed", device_id=session.device_id, error=str(exc))
            entry.tools = {}
            return
        accepted = self._accept_tools(listed.get("tools"))
        policy, device_id, reported = self.policy, session.device_id, reported_groups(listed)

        def record() -> tuple[dict[str, str], set[str]]:
            tools = policy.record_listing(device_id, accepted, reported=reported, complete=complete)
            return tools, policy.narrowed(device_id)

        # A narrowed group is filtered here as well as on the node: nothing is routed to it even
        # in the moment between a new connection and the node applying `nox/policy`.
        tools, narrowed = await asyncio.to_thread(record)
        entry.narrowed = frozenset(narrowed)
        # A group with a narrowing still on its way stays listed here but is not offered
        # (`NodeEntry.offers`); once that request is done, the list is right either way.
        entry.tools = tools
        if narrowed:
            await send_policy(session, narrowed)
        log.info("node.tools", device_id=session.device_id, tools=sorted(entry.tools))

    async def set_narrowed(self, device_id: str, group: str, narrowed: bool) -> None:
        """Narrow (or clear) one group of one node. Narrowing takes effect on the core at once,
        before the request waits for anything; the node is told as soon as it is connected.

        Requests for the same group are applied in arrival order, and the last to arrive wins: a
        request older than the last one applied for that group changes nothing. While any
        narrowing of the group is still on its way, the group is off, whatever else finished.

        Once asked, it is applied to the end: the apply runs as a task of its own, shielded from
        the caller - a dashboard that disconnects (and so cancels its request) can never leave
        the database saying "off" while the core still routes the group.
        """
        seq = next(self._seq)  # stamped before the first await: arrival order
        marked = self._entries.get(device_id) if narrowed else None
        if marked is not None:
            marked.pending_narrow[group] += 1
        task = asyncio.ensure_future(self._apply(device_id, group, narrowed, seq, marked))
        self._applying.add(task)
        task.add_done_callback(self._applied_done)
        await asyncio.shield(task)

    def _applied_done(self, task: asyncio.Future[None]) -> None:
        self._applying.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("node.narrow_failed", error=type(task.exception()).__name__)

    async def _apply(
        self, device_id: str, group: str, narrowed: bool, seq: int, marked: NodeEntry | None
    ) -> None:
        try:
            async with self._lock(device_id):
                key = (device_id, group)
                if self._applied.get(key, 0) > seq:
                    return  # a later request for this group was applied already
                self._applied[key] = seq
                await asyncio.to_thread(self.policy.set_narrowed, device_id, group, narrowed)
                now = await asyncio.to_thread(self.policy.narrowed, device_id)
                entry = self._entries.get(device_id)  # it may have connected meanwhile
                if entry is None:
                    return
                entry.narrowed = frozenset(now)
                entry.tools = {t: r for t, r in entry.tools.items() if group_of(t) not in now}
                session = entry.session
                if session is None or not entry.online:
                    return
                await send_policy(session, now)
                if not narrowed:
                    await self._refresh(session, complete=False)  # it lists the tools again
        finally:
            # Only this request's own marker, when the apply itself is over (also on an error).
            if marked is not None:
                marked.pending_narrow[group] -= 1
                if marked.pending_narrow[group] <= 0:
                    del marked.pending_narrow[group]

    @staticmethod
    def _accept_tools(raw: Any) -> dict[str, str]:
        accepted: dict[str, str] = {}
        for item in raw if isinstance(raw, list) else []:
            if len(accepted) >= MAX_TOOLS:
                break
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            name = item["name"]
            if name not in ROUTABLE or matching_hard_prohibition(name) is not None:
                continue
            nox = item.get("_nox")
            risk = nox.get("risk") if isinstance(nox, dict) else None
            accepted[name] = risk if risk in RISKS else "high"
        return accepted

    # -- what the router asks ------------------------------------------------------------------

    def _entry(self, device_id: str, name: str) -> NodeEntry:
        # The name was typed on the device: only its plain form is ever kept, so every result,
        # error and candidate list the model sees carries that and nothing else.
        shown = display_name(name)
        entry = self._entries.get(device_id)
        if entry is None:
            entry = self._entries[device_id] = NodeEntry(device_id=device_id, name=shown)
        entry.name = shown
        return entry

    def entries(self) -> list[NodeEntry]:
        """Every paired node, online or not (an offline one is still a name the user may say)."""
        known = {d.id: d for d in self._pairing.nodes() if not d.revoked}
        for device_id in list(self._entries):
            if device_id not in known:
                del self._entries[device_id]  # revoked: no longer a device at all
        for device in known.values():
            entry = self._entry(device.id, device.name)
            if entry.last_seen is None and device.last_seen_at is not None:
                entry.last_seen = device.last_seen_at
            row = self._nodes.get_node(device.id)
            entry.linked_device_id = row.linked_device_id if row is not None else None
        return sorted(self._entries.values(), key=lambda e: e.name.lower())

    def online_with(self, tool: str) -> list[NodeEntry]:
        return [e for e in self.entries() if e.offers(tool)]

    def any_online_with(self, tool: str) -> bool:
        return any(e.offers(tool) for e in self._entries.values())

    async def call(
        self,
        entry: NodeEntry,
        tool: str,
        arguments: dict[str, Any],
        *,
        risk: str,
        timeout_s: float,
        confirm_on_device: bool = False,
    ) -> dict[str, Any]:
        """One call on one node. Offline at this moment: fail fast, nothing is queued."""
        if entry.connecting:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(entry.listed.wait(), LISTING_WAIT_S)
        session = entry.session
        if session is None or not entry.offers(tool):
            return offline_error(entry)
        call_id = uuid.uuid4().hex
        decision = CALL_DECISION.get()
        decided_by = decision.decided_by if decision is not None else "profile"
        tainted = decision.tainted if decision is not None else False
        issued_at = time.time()
        deadline = issued_at + min(timeout_s, MAX_LIFETIME_S)
        meta: dict[str, Any] = {
            "call_id": call_id,
            "risk": risk,
            "deadline": deadline,
            "issued_at": issued_at,
            "decided_by": decided_by,
            "confirm_on_device": confirm_on_device,
            "tainted": tainted,
        }
        if self._signer is not None:
            try:
                meta["receipt"] = self._signer.sign(
                    call_id=call_id,
                    device_id=entry.device_id,
                    core_id=self._core_id(),
                    tool=tool,
                    arguments=arguments,
                    risk=risk,
                    decided_by=decided_by,
                    confirm_on_device=confirm_on_device,
                    tainted=tainted,
                    issued_at=issued_at,
                    deadline=deadline,
                )
            except ValueError:
                return {"error": "invalid_arguments", "device": entry.label}
        if confirm_on_device:
            timeout_s += CONFIRM_TIMEOUT_S  # the person at the device has a minute to answer
        try:
            raw = await session.client.request(
                "tools/call",
                {"name": tool, "arguments": arguments, "_nox": meta},
                timeout_s=timeout_s,
            )
        except (TimeoutError, McpClientError) as exc:
            log.warning("node.call_failed", device_id=entry.device_id, tool=tool, error=str(exc))
            if not entry.online or session.client.closed:
                return offline_error(entry)
            return {"error": "failed_on_device", "detail": "no answer", "device": entry.label}
        return clean_result(raw, device=entry.label)

    def prompt_line(self) -> str:
        """One line for the turn context: which devices exist and whether they are reachable."""
        entries = self.entries()
        if not entries:
            return ""
        parts = ["dieser Core (core)"]
        for entry in entries:
            if entry.online and entry.compatible:
                state = "online"
            elif entry.online:
                state = "online, Update nötig"
            elif entry.last_seen is not None:
                state = f"offline seit {entry.last_seen.astimezone():%d.%m. %H:%M}"
            else:
                state = "offline"
            parts.append(f"{entry.label} ({state})")
        return "Geräte: " + ", ".join(parts)


def offline_error(entry: NodeEntry) -> dict[str, Any]:
    seen = entry.last_seen.isoformat() if entry.last_seen is not None else None
    return {"error": "device_offline", "device": entry.label, "last_seen": seen}
