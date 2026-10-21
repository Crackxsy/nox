"""`nox node run`: the node's connection to its core, kept up for as long as the node runs.

The node dials out (`wss://<core>:<node_port>/node/v1`), with its client certificate, trusting
only the authority it pinned at pairing; the core never connects to a node. It introduces itself
with `initialize`, then serves the core as an MCP server: `tools/list` lists what `NodeGate`
offers, `tools/call` runs only what `NodeGate` allows. Everything the core sends outside
`CORE_TO_NODE` is refused.

Connection lost: reconnect with backoff, 1 s doubling to 60 s, and never act on its own meanwhile.
`nox/revoked`: the node deletes its key and certificate and stops ("Von Nox entfernt").
The node pings every second; the core's pings are answered.

Stopping (spec C §1.7):
* `nox/kill` from the core: every running call is cancelled at once, the node answers
  `nox/killed` with their ids and refuses everything until the core's `nox/resume`.
* Dead man: while a call with a side effect runs and nothing came from the core for
  `DEAD_MAN_S`, that call is aborted and the node refuses everything until the core is back and
  not stopped.
* Stopped on this device (`nox node stop`): the same, plus `security.kill` to the core - which
  stops Nox everywhere. Only this device resumes it (`nox node resume`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from nox import __version__
from nox.core.logging import get_logger
from nox.mcp.protocol import PROTOCOL_VERSION
from nox.node import protocol as p
from nox.node.confirm import Confirmer, PlatformConfirmer
from nox.node.enroll import local_os
from nox.node.gate import NodeGate
from nox.node.home import NodeHome, NodeIdentity
from nox.node.keystore import NodeKeyStore
from nox.node.manifest import GROUPS, NodeManifest, manifest_hash
from nox.node.receipts import ReceiptVerifier
from nox.node.tools import NodeTool
from nox.security.model import AuditLog

log = get_logger(__name__)

__all__ = ["BACKOFF_MAX_S", "NodeAgent", "Outcome"]

BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0
PING_INTERVAL_S = 1.0
HELLO_TIMEOUT_S = 5.0
#: Largest result sent in one frame; the protocol caps frames at 1 MiB.
MAX_RESULT_CHARS = 512 * 1024
#: No word from the core for this long while a side effect runs: abort it (spec C §1.7).
DEAD_MAN_S = 3.0
DEAD_MAN_TICK_S = 0.25
LOCAL_STOP_POLL_S = 0.2
#: Calls running on this node at the same time, at most.
MAX_CONCURRENT_CALLS = 8
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602

#: How `run` ended: stopped locally, removed by the core, or never paired.
Outcome = str
Connector = Callable[..., Awaitable[ClientConnection]]


#: Longest question the dialog shows; anything longer is refused, never cut.
MAX_CONFIRM_CHARS = 1200


def confirmation_text(tool: str, arguments: dict[str, Any]) -> str | None:
    """The question for the person at the device: every argument in full, control characters
    escaped, one per line. None when it would be too long to read - then nothing is asked and the
    call is refused (no silent truncation)."""
    lines = [f"Nox möchte auf diesem Gerät „{p.escape_controls(tool)}“ ausführen:"]
    for key, value in arguments.items():
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"  {p.escape_controls(str(key))}: {p.escape_controls(str(shown))}")
    lines.append("Erlauben? (Ohne Antwort in 60 s: Ablehnen)")
    text = "\n".join(lines)
    return text if len(text) <= MAX_CONFIRM_CHARS else None


class DeadManError(Exception):
    """The core went silent while a side effect ran."""


class NodeAgent:
    def __init__(
        self,
        home: NodeHome,
        keystore: NodeKeyStore,
        gate: NodeGate,
        audit: AuditLog | None,
        *,
        connector: Connector = connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ping_interval_s: float = PING_INTERVAL_S,
        manifest_source: Callable[[], NodeManifest] | None = None,
        confirmer: Confirmer | None = None,
    ) -> None:
        self._confirmer: Confirmer = confirmer if confirmer is not None else PlatformConfirmer()
        self.core_killed = False
        self.dead_man = False
        self._local_stopped = False
        #: call id -> (the running call, its JSON-RPC id)
        self._calls: dict[str, tuple[asyncio.Task[None], Any]] = {}
        self._last_core = time.monotonic()
        gate.stop_state = self.stopped_reason
        self._manifest_source = manifest_source
        self._manifest_mtime: float | None = None
        self._home = home
        self._keystore = keystore
        self._gate = gate
        self._audit = audit
        self._connect = connector
        self._sleep = sleep
        self._ping_interval_s = ping_interval_s
        self.connected = asyncio.Event()
        self.compatible = True
        self.attempts = 0

    # -- the loop --------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event | None = None) -> Outcome:
        stop = stop or asyncio.Event()
        identity = self._home.load_identity()
        if identity is None or not self._home.paired:
            return "not_paired"
        if not self._home.pinned_ca_ok(identity):
            # The stored authority is not the pinned one: never accept another (spec C §1.8).
            log.error("node.pin_mismatch")
            self._status("pin_mismatch", identity)
            return "pin_mismatch"
        delay = BACKOFF_START_S
        while not stop.is_set():
            self.attempts += 1
            outcome = await self._once(identity, stop)
            if outcome in ("revoked", "stopped"):
                return outcome
            if outcome == "session":
                delay = BACKOFF_START_S  # it worked for a while: start over
            self._status("disconnected", identity)
            wait = delay * (0.8 + 0.4 * random.random())  # noqa: S311 - jitter, not crypto
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wait(stop, wait), wait + 1)
            delay = min(delay * 2, BACKOFF_MAX_S)
        return "stopped"

    async def _wait(self, stop: asyncio.Event, seconds: float) -> None:
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        stopper = asyncio.ensure_future(stop.wait())
        _done, pending = await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

    async def _once(self, identity: NodeIdentity, stop: asyncio.Event) -> Outcome:
        host = f"[{identity.core_host}]" if ":" in identity.core_host else identity.core_host
        try:
            context = self._keystore.client_context(
                ca_file=self._home.ca_file, cert_file=self._home.cert_file
            )
            ws = await self._connect(
                f"wss://{host}:{identity.node_port}{p.NODE_PATH}",
                ssl=context,
                server_hostname=identity.core_host,
                open_timeout=10,
                ping_interval=None,
                compression=None,
                max_size=p.MAX_FRAME_BYTES,
            )
        except InvalidStatus as exc:
            log.warning("node.refused_by_core", status=exc.response.status_code)
            return "refused"
        except (OSError, InvalidHandshake, TimeoutError, ValueError) as exc:
            log.info("node.connect_failed", error=type(exc).__name__)
            return "failed"
        try:
            return await self._session(ws, identity, stop)
        finally:
            self.connected.clear()
            with contextlib.suppress(Exception):
                await ws.close()

    # -- one connection ----------------------------------------------------------------------------

    def _hello(self) -> dict[str, Any]:
        offered = self._gate.offered()
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": p.M_INITIALIZE,
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "node_protocol": p.NODE_PROTOCOL,
                "node_version": __version__[:32],
                "os": local_os(),
                "arch": (platform.machine() or "unknown")[:16].replace("-", "_"),
                "manifest_hash": manifest_hash(
                    self._gate.manifest.enabled(), [t.name for t in offered]
                ),
                "confirmer": bool(self._confirmer.available),
            },
        }

    async def _session(
        self, ws: ClientConnection, identity: NodeIdentity, stop: asyncio.Event
    ) -> Outcome:
        await ws.send(json.dumps(self._hello()))
        try:
            answer = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_S))
        except (TimeoutError, ConnectionClosed, ValueError):
            return "failed"
        result = answer.get("result") if isinstance(answer, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("node_id") != identity.node_id
            or result.get("core_id") != identity.ca_fingerprint
        ):
            log.error("node.unexpected_core")
            return "failed"
        if not self._pin_receipt_key(identity, result.get("receipt_key")):
            log.error("node.receipt_key_changed")
            return "failed"
        self.compatible = bool(result.get("compatible", False))
        self.core_killed = bool(result.get("engaged", False))
        if not self.core_killed:
            self.dead_man = False  # the core is back and not stopped
        self._last_core = time.monotonic()
        self._gate.narrowed = set()
        self.connected.set()
        self._status("connected" if self.compatible else "update_needed", identity)
        log.info("node.connected", compatible=self.compatible)
        pinger = asyncio.create_task(self._ping(ws))
        watcher = asyncio.create_task(self._watch_local_stop(ws))
        stopper = asyncio.create_task(stop.wait())
        try:
            while True:
                receiver = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait(
                    {receiver, stopper}, return_when=asyncio.FIRST_COMPLETED
                )
                if stopper in done:
                    receiver.cancel()
                    return "stopped"
                try:
                    frame = receiver.result()
                except ConnectionClosed:
                    return "session"
                self._last_core = time.monotonic()
                if not isinstance(frame, str):
                    return "session"  # binary frames are not part of the protocol
                try:
                    message = json.loads(frame)
                except ValueError:
                    return "session"
                if not isinstance(message, dict):
                    return "session"
                outcome = await self._dispatch(ws, message)
                if outcome is not None:
                    return outcome
        finally:
            pinger.cancel()
            watcher.cancel()
            stopper.cancel()
            # The connection is gone: nothing keeps running without the core.
            await self._cancel_calls()

    def reload_manifest(self) -> bool:
        """Re-read `node.yaml` when it changed; True when what the node offers changed."""
        if self._manifest_source is None:
            return False
        try:
            mtime = self._home.manifest_file.stat().st_mtime
        except OSError:
            return False
        if mtime == self._manifest_mtime:
            return False
        self._manifest_mtime = mtime
        before = [t.name for t in self._gate.offered()]
        try:
            self._gate.set_manifest(self._manifest_source())
        except ValueError as exc:
            # A broken file allows nothing rather than everything.
            log.error("node.manifest_unreadable", error=str(exc)[:200])
            self._gate.set_manifest(NodeManifest(groups={g: False for g in GROUPS}))
        return [t.name for t in self._gate.offered()] != before

    async def _ping(self, ws: ClientConnection) -> None:
        seq = 0
        self.reload_manifest()
        while True:
            await asyncio.sleep(self._ping_interval_s)
            seq += 1
            if self.reload_manifest():
                with contextlib.suppress(ConnectionClosed):
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": p.M_LIST_CHANGED}))
            with contextlib.suppress(ConnectionClosed):
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": p.M_PING,
                            "params": {"seq": seq, "ts": time.time()},
                        }
                    )
                )

    async def _reply(
        self,
        ws: ClientConnection,
        msg_id: Any,
        *,
        result: Any = None,
        error: tuple[int, str] | None = None,
    ) -> None:
        if msg_id is None:
            return
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            message["error"] = {"code": error[0], "message": error[1]}
        else:
            message["result"] = result if result is not None else {}
        with contextlib.suppress(ConnectionClosed):
            await ws.send(json.dumps(message, ensure_ascii=False))

    async def _dispatch(self, ws: ClientConnection, message: dict[str, Any]) -> Outcome | None:
        method = message.get("method")
        msg_id = message.get("id")
        if not isinstance(method, str):
            return None  # an answer to one of our pings, or noise: nothing to do
        if method not in p.CORE_TO_NODE:
            await self._reply(ws, msg_id, error=(ERR_METHOD_NOT_FOUND, "not supported"))
            return None
        raw = message.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if method == p.M_PING:
            with contextlib.suppress(ValidationError, ConnectionClosed):
                ping = p.Ping.model_validate(params)
                if self._gate.receipts is not None:
                    self._gate.receipts.observe_core_time(ping.ts)
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": p.M_PONG,
                            "params": {"seq": ping.seq, "ts": time.time()},
                        }
                    )
                )
            return None
        if method == p.M_PONG:
            return None
        if method == p.M_REVOKED:
            self._forget("revoked by the core")
            return "revoked"
        if method == p.M_POLICY:
            groups = params.get("narrow")
            if isinstance(groups, list):
                self._gate.narrow([g for g in groups if isinstance(g, str)][:32])
            await self._reply(
                ws, msg_id, result={"offered": [t.name for t in self._gate.offered()]}
            )
            return None
        if method == p.M_TOOLS_LIST:
            offered = (
                self._gate.offered()
                if self.compatible
                else [t for t in self._gate.offered() if t.name == "system.info"]
            )
            await self._reply(ws, msg_id, result={"tools": [t.describe() for t in offered]})
            return None
        if method == p.M_TOOLS_CALL:
            if len(self._calls) >= MAX_CONCURRENT_CALLS:
                # A flood of calls (even reads) cannot exhaust the device.
                await self._refused(ws, msg_id, str(params.get("name", "call")), "busy")
                return None
            meta = params.get("_nox")
            call_id = meta.get("call_id") if isinstance(meta, dict) else None
            key = (
                call_id if isinstance(call_id, str) and call_id not in self._calls else f"#{msg_id}"
            )
            task = asyncio.create_task(self._call(ws, msg_id, params))
            self._calls[key] = (task, msg_id)

            def forget(_done: asyncio.Task[None], key: str = key) -> None:
                self._calls.pop(key, None)

            task.add_done_callback(forget)
            return None
        if method == p.M_KILL:
            self.core_killed = True
            stopped = await self._cancel_calls(ws)
            self._audit_event("node.kill", "core", f"stopped {len(stopped)} call(s)")
            with contextlib.suppress(ConnectionClosed):
                await ws.send(
                    json.dumps(
                        {"jsonrpc": "2.0", "method": p.M_KILLED, "params": {"call_ids": stopped}}
                    )
                )
            await self._reply(ws, msg_id, result={"call_ids": stopped})
            return None
        if method == p.M_RESUME:
            # Only the core's own stop ends here; a stop on this device stays until it is resumed
            # on this device.
            self.core_killed = False
            self.dead_man = False
            self._audit_event("node.resume", "core", "ok")
            await self._reply(ws, msg_id, result={"stopped": self.stopped_reason()})
            return None
        await self._reply(ws, msg_id, error=(ERR_METHOD_NOT_FOUND, "not supported"))
        return None

    # -- stopping ------------------------------------------------------------------------------

    def stopped_reason(self) -> str | None:
        """Why this node refuses everything right now, or None."""
        if self._local_stopped or self._home.stop_file.exists():
            return "stopped_on_device"
        if self.core_killed:
            return "stopped_by_core"
        if self.dead_man:
            return "dead_man"
        return None

    async def _cancel_calls(self, ws: ClientConnection | None = None) -> list[str]:
        """Cancel every running call; with `ws`, each is answered "stopped". Their ids."""
        running = dict(self._calls)
        for task, _msg_id in running.values():
            task.cancel()
        for task, _msg_id in running.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if ws is not None:
            for _task, msg_id in running.values():
                await self._refused(ws, msg_id, "call", "stopped")
        return [k for k in running if not k.startswith("#")]

    async def _watch_local_stop(self, ws: ClientConnection) -> None:
        """`nox node stop` on this device: stop here at once, and ask the core to stop all."""
        while True:
            stopped = self._home.stop_file.exists()
            if stopped and not self._local_stopped:
                self._local_stopped = True
                calls = await self._cancel_calls(ws)
                self._audit_event("node.local_stop", "local", f"stopped {len(calls)} call(s)")
                with contextlib.suppress(ConnectionClosed):
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": p.M_SECURITY_KILL,
                                "params": {"reason": "stopped on the device"},
                            }
                        )
                    )
            elif not stopped and self._local_stopped:
                self._local_stopped = False
                self._audit_event("node.local_resume", "local", "ok")
            await asyncio.sleep(LOCAL_STOP_POLL_S)

    def _pin_receipt_key(self, identity: NodeIdentity, offered: Any) -> bool:
        """The core's receipt key: pinned at pairing; adopted once over the pinned connection by
        a node paired before receipts existed; never replaced."""
        key = offered if isinstance(offered, str) and offered else ""
        if identity.receipt_key:
            if key != identity.receipt_key:
                return False
        elif key:
            identity.receipt_key = key
            self._home.save_identity(identity)
        try:
            self._gate.receipts = (
                ReceiptVerifier(identity.receipt_key) if identity.receipt_key else None
            )
        except ValueError:
            return False
        return True

    def _audit_event(self, action: str, by: str, result: str) -> None:
        if self._audit is not None:
            self._audit.append(
                actor=by, tool="node", action=action, target="", decision="allow", result=result
            )

    async def _call(self, ws: ClientConnection, msg_id: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        try:
            meta = p.NoxCallMeta.model_validate(params.get("_nox") or {})
        except ValidationError:
            await self._reply(ws, msg_id, error=(ERR_INVALID_PARAMS, "missing or invalid _nox"))
            return
        if not isinstance(name, str):
            await self._reply(ws, msg_id, error=(ERR_INVALID_PARAMS, "missing tool name"))
            return
        if not self.compatible and name != "system.info":
            decision_reason = "update_needed"
            await self._refused(ws, msg_id, name, decision_reason)
            return
        decision = self._gate.check(name, params.get("arguments"), call_id=meta.call_id, meta=meta)
        if not decision.allowed or decision.tool is None or decision.args is None:
            await self._refused(ws, msg_id, name, decision.reason)
            return
        if decision.needs_local_confirmation:
            refusal = await self._confirm_here(ws, decision.tool, decision.args, meta)
            if refusal is not None:
                await self._refused(ws, msg_id, name, refusal)
                return
        try:
            work = decision.tool.handler(decision.args)
            if decision.tool.risk != "read":
                result = await self._under_dead_man(work)
            else:
                result = await asyncio.wait_for(work, 30)
        except DeadManError:
            self.dead_man = True
            self._audit_event("node.dead_man", "local", p.safe_name(name))
            await self._refused(ws, msg_id, name, "dead_man")
            return
        except Exception as exc:  # noqa: BLE001 - a failing tool is an error result, not a crash
            log.warning("node.tool_failed", tool=p.safe_name(name), error=type(exc).__name__)
            await self._reply(
                ws,
                msg_id,
                result={
                    "isError": True,
                    "content": [{"type": "text", "text": f"{type(exc).__name__}"}],
                },
            )
            return
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            # Too large for one frame: an honest error rather than a cut result or a broken link.
            await self._reply(
                ws,
                msg_id,
                result={"isError": True, "content": [{"type": "text", "text": "result too large"}]},
            )
            return
        await self._reply(
            ws,
            msg_id,
            result={"content": [{"type": "text", "text": text}], "structuredContent": result},
        )

    async def _under_dead_man(self, work: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        """Run a call with a side effect; abort it when the core went silent for `DEAD_MAN_S`."""
        task: asyncio.Task[dict[str, Any]] = asyncio.ensure_future(work)
        try:
            deadline = time.monotonic() + 30
            while True:
                done, _ = await asyncio.wait({task}, timeout=DEAD_MAN_TICK_S)
                if done:
                    return task.result()
                if time.monotonic() - self._last_core > DEAD_MAN_S:
                    raise DeadManError
                if time.monotonic() > deadline:
                    raise TimeoutError
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _confirm_here(
        self, ws: ClientConnection, tool: NodeTool, args: BaseModel, meta: p.NoxCallMeta
    ) -> str | None:
        """High risk: the person at this device decides (D1). None when they allowed it."""
        if not self._confirmer.available:
            if self._gate.manifest.trust_core_for_high:
                self._audit_event("node.confirm", "core", "trusted (no screen)")
                return None
            return "high_needs_local_confirmation"
        text = confirmation_text(tool.name, args.model_dump())
        if text is None:
            # Never ask about something the person cannot see in full.
            self._audit_event("node.confirm", "local", f"{p.safe_name(tool.name)}:too_long")
            return "too_long_to_confirm"
        try:
            answer = await self._confirmer.ask(text)
        except Exception as exc:  # noqa: BLE001 - a broken dialog is a "no", never a "yes"
            log.warning("node.confirm_failed", error=type(exc).__name__)
            answer = "deny"
        self._audit_event("node.confirm", "local", f"{p.safe_name(tool.name)}:{answer}")
        with contextlib.suppress(ConnectionClosed):
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": p.M_CONFIRM_RESULT,
                        "params": {"call_id": meta.call_id, "decision": answer},
                    }
                )
            )
        if answer != "allow":
            return "denied_on_device" if answer == "deny" else "confirmation_timeout"
        if self.stopped_reason() is not None:
            return self.stopped_reason()  # stopped while the dialog was open
        return None

    async def _refused(self, ws: ClientConnection, msg_id: Any, name: str, reason: str) -> None:
        log.warning("node.call_refused", tool=p.safe_name(name, 64), reason=reason)
        await self._reply(
            ws,
            msg_id,
            result={
                "isError": True,
                "_nox": {"refused": reason},
                "content": [{"type": "text", "text": f"refused on the device: {reason}"}],
            },
        )

    # -- helpers -------------------------------------------------------------------------------

    def _forget(self, why: str) -> None:
        if self._audit is not None:
            self._audit.append(
                actor="core",
                tool="node",
                action="node.revoked",
                target="",
                decision="allow",
                result=why,
            )
        self._keystore.delete()
        self._home.forget()
        self._home.write_status({"state": "removed", "at": datetime.now(UTC).isoformat()})
        log.warning("node.removed_by_core")

    def _status(self, state: str, identity: NodeIdentity) -> None:
        self._home.write_status(
            {
                "state": state,
                "at": datetime.now(UTC).isoformat(),
                "core": f"{identity.core_host}:{identity.node_port}",
                "name": identity.name,
                "offered": [t.name for t in self._gate.offered()],
            }
        )
