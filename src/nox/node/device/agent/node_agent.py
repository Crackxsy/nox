"""`NodeAgent`, the top layer: the connection to the core, its backoff, one session and the
dispatch of the core's messages."""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import random
import time
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from nox import __version__
from nox.core.logging import get_logger
from nox.mcp.protocol import PROTOCOL_VERSION
from nox.node.device.agent.base import ERR_INVALID_PARAMS, ERR_METHOD_NOT_FOUND
from nox.node.device.agent.core_requests import CoreRequests
from nox.node.device.agent.netinfo import ADDRESSES_TTL_S, local_addresses
from nox.node.device.enroll import local_os
from nox.node.device.home import NodeIdentity
from nox.node.wire import protocol as p
from nox.node.wire.manifest import GROUPS, NodeManifest, manifest_hash
from nox.node.wire.receipts import ReceiptVerifier

log = get_logger(__name__)


BACKOFF_START_S = 1.0


BACKOFF_MAX_S = 60.0


HELLO_TIMEOUT_S = 5.0


#: Calls running on this node at the same time, at most.
MAX_CONCURRENT_CALLS = 8


#: How `run` ended: stopped locally, removed by the core, or never paired.
Outcome = str


class NodeAgent(CoreRequests):
    """The node's connection to its core: `run` keeps it up with backoff, one session answers the
    core's messages, and `_dispatch` serves only the methods in `CORE_TO_NODE`."""

    async def run(self, stop: asyncio.Event | None = None) -> Outcome:
        stop = stop or asyncio.Event()
        identity = self._home.load_identity()
        if identity is None or not self._home.paired:
            return "not_paired"
        if not self._home.pinned_ca_ok(identity):
            # The stored authority is not the pinned one: never accept another.
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
                "addresses": self._addresses_now(),
                # The satellite methods this node understands from a core.
                "features": sorted(p.FEATURES),
                **self._session_field(),
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
        self.protocol_refused = not self.compatible
        self._remember_core_protocol(identity, result.get("node_protocol"))
        self.core_killed = bool(result.get("engaged", False))
        if not self.core_killed:
            self.dead_man = False  # the core is back and not stopped
        self._last_core = time.monotonic()
        self._gate.narrowed = set()
        if self.compatible:
            # Only a core that accepts this node counts as connected: a version on trial is
            # confirmed by nothing less (`nox.node.device.cli.keep_updated`).
            self.connected.set()
        self._status("connected" if self.compatible else "update_needed", identity)
        log.info("node.connected", compatible=self.compatible)
        offered = result.get("features")
        self.features = (
            frozenset(f for f in offered[:16] if isinstance(f, str) and f in p.FEATURES)
            if isinstance(offered, list) and self.compatible
            else frozenset()
        )
        self._ws = ws
        self._tell_connection(self.compatible)
        pinger = asyncio.create_task(self._ping(ws))
        watcher = asyncio.create_task(self._watch_local_stop(ws))
        renewer = asyncio.create_task(self._renew_loop(ws, identity))
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
            renewer.cancel()
            stopper.cancel()
            for waiting in self._pending.values():
                waiting.cancel()
            self._pending.clear()
            self._ws = None
            self.features = frozenset()
            self._tell_connection(False)
            # The connection is gone: nothing keeps running without the core.
            await self._cancel_calls()

    def _tell_connection(self, connected: bool) -> None:
        for listener in list(self.on_connection):
            try:
                listener(connected)
            except Exception as exc:  # noqa: BLE001 - a satellite never breaks the connection
                log.warning("node.connection_listener_failed", error=type(exc).__name__)

    def _groups(self) -> dict[str, list[str]]:
        """What this device enabled, said outright (the core shows it; it widens nothing): the
        core cannot infer it from the tool list, which leaves out a narrowed group and every tool
        above `max_risk`."""
        manifest = self._gate.manifest
        folders = [str(f) for f in manifest.folders][:32] if "files" in manifest.enabled() else []
        # The folders `files` may open here, for the Devices page (the core never routes by them).
        return {"groups": sorted(manifest.enabled()), "folders": folders}

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

    async def _dispatch(self, ws: ClientConnection, message: dict[str, Any]) -> Outcome | None:
        method = message.get("method")
        msg_id = message.get("id")
        if not isinstance(method, str):
            waiting = self._pending.pop(str(msg_id), None) if msg_id is not None else None
            if waiting is not None and not waiting.done():
                waiting.set_result(message)
            return None  # otherwise an answer to one of our pings, or noise: nothing to do
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
                self._after_core_ping()
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": p.M_PONG,
                            "params": {
                                "seq": ping.seq,
                                "ts": time.time(),
                                "state": self.stopped_reason() or "",
                                "addresses": self._addresses_now(),
                                **self._session_field(),
                            },
                        }
                    )
                )
            return None
        if method == p.M_PONG:
            return None
        if method in p.SATELLITE_TO_NODE:
            handler = self.satellites.get(method)
            if handler is None:
                await self._reply(ws, msg_id, error=(ERR_METHOD_NOT_FOUND, "not supported"))
                return None
            try:
                await handler(params)
            except Exception as exc:  # noqa: BLE001 - a satellite never breaks the connection
                log.warning("node.satellite_failed", method=method, error=type(exc).__name__)
            await self._reply(ws, msg_id, result={})
            return None
        if method == p.M_REVOKED:
            self._forget("revoked by the core")
            return "revoked"
        if method == p.M_POLICY:
            groups = params.get("narrow")
            if isinstance(groups, list):
                self._gate.narrow([g for g in groups if isinstance(g, str)][:32])
            await self._reply(
                ws,
                msg_id,
                result={"offered": [t.name for t in self._gate.offered()], "_nox": self._groups()},
            )
            return None
        if method == p.M_GAMES:
            try:
                games = p.GamesAdd.model_validate(params)
            except ValidationError:
                await self._reply(ws, msg_id, error=(ERR_INVALID_PARAMS, "invalid games"))
                return None
            self.on_games(list(games.add))
            self._audit_event("node.games", "core", f"added {len(games.add)}")
            await self._reply(ws, msg_id, result={"ok": True})
            return None
        if method == p.M_TOOLS_LIST:
            offered = (
                self._gate.offered()
                if self.compatible
                else [t for t in self._gate.offered() if t.name == "system.info"]
            )
            result = {"tools": [t.describe() for t in offered], "_nox": self._groups()}
            await self._reply(ws, msg_id, result=result)
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
            self._calls[key] = (task, msg_id, str(params.get("name", "")))

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

    def _remember_core_protocol(self, identity: NodeIdentity, offered: Any) -> None:
        """Keep the range of node protocols the core accepts, for deciding about updates."""
        if not (
            isinstance(offered, list)
            and len(offered) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in offered)
            and 0 < offered[0] <= offered[1] <= 1000
        ):
            return
        span = (offered[0], offered[1])
        if identity.core_node_protocol != span:
            identity.core_node_protocol = span
            self._home.save_identity(identity)

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
                ReceiptVerifier(
                    identity.receipt_key,
                    device_id=identity.node_id,
                    core_id=identity.ca_fingerprint,
                )
                if identity.receipt_key
                else None
            )
        except ValueError:
            return False
        return True

    def _addresses_now(self) -> list[str]:
        """`local_addresses`, reused for `ADDRESSES_TTL_S`."""
        now = time.monotonic()
        cached = getattr(self, "_addresses_cache", None)
        if cached is None or now - cached[0] > ADDRESSES_TTL_S:
            cached = (now, local_addresses())
            self._addresses_cache = cached
        return list(cached[1])

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
