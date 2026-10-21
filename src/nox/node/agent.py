"""`nox node run`: the node's connection to its core, kept up for as long as the node runs.

The node dials out (`wss://<core>:<node_port>/node/v1`), with its client certificate, trusting
only the authority it pinned at pairing; the core never connects to a node. It introduces itself
with `initialize`, then serves the core as an MCP server: `tools/list` lists what `NodeGate`
offers, `tools/call` runs only what `NodeGate` allows. Everything the core sends outside
`CORE_TO_NODE` is refused.

Connection lost: reconnect with backoff, 1 s doubling to 60 s, and never act on its own meanwhile.
`nox/revoked`: the node deletes its key and certificate and stops ("Von Nox entfernt").
The node pings every second; the core's pings are answered.
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

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from nox import __version__
from nox.core.logging import get_logger
from nox.mcp.protocol import PROTOCOL_VERSION
from nox.node import protocol as p
from nox.node.enroll import local_os
from nox.node.gate import NodeGate
from nox.node.home import NodeHome, NodeIdentity
from nox.node.keystore import NodeKeyStore
from nox.node.manifest import manifest_hash
from nox.security.model import AuditLog

log = get_logger(__name__)

__all__ = ["BACKOFF_MAX_S", "NodeAgent", "Outcome"]

BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0
PING_INTERVAL_S = 1.0
HELLO_TIMEOUT_S = 5.0
#: Largest result sent in one frame; the protocol caps frames at 1 MiB.
MAX_RESULT_CHARS = 512 * 1024
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602

#: How `run` ended: stopped locally, removed by the core, or never paired.
Outcome = str
Connector = Callable[..., Awaitable[ClientConnection]]


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
    ) -> None:
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
        self.compatible = bool(result.get("compatible", False))
        self._gate.narrowed = set()
        self.connected.set()
        self._status("connected" if self.compatible else "update_needed", identity)
        log.info("node.connected", compatible=self.compatible)
        pinger = asyncio.create_task(self._ping(ws))
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
            stopper.cancel()

    async def _ping(self, ws: ClientConnection) -> None:
        seq = 0
        while True:
            await asyncio.sleep(self._ping_interval_s)
            seq += 1
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
            await self._call(ws, msg_id, params)
            return None
        # nox/kill, nox/resume: the kill path arrives with the node gate's next step (C4).
        await self._reply(ws, msg_id, error=(ERR_METHOD_NOT_FOUND, "not supported yet"))
        return None

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
        decision = self._gate.check(name, params.get("arguments"), call_id=meta.call_id)
        if not decision.allowed or decision.tool is None or decision.args is None:
            await self._refused(ws, msg_id, name, decision.reason)
            return
        try:
            result = await asyncio.wait_for(decision.tool.handler(decision.args), 30)
        except Exception as exc:  # noqa: BLE001 - a failing tool is an error result, not a crash
            log.warning("node.tool_failed", tool=name, error=type(exc).__name__)
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

    async def _refused(self, ws: ClientConnection, msg_id: Any, name: str, reason: str) -> None:
        log.warning("node.call_refused", tool=name[:64], reason=reason)
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
