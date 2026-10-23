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
import base64
import contextlib
import json
import platform
import random
import time
import unicodedata
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
from nox.node.receipts import ReceiptVerifier, normalize_arguments
from nox.node.renewal import RenewalRefusedError, accept_renewed, due, store_certificate
from nox.node.tools import NodeRefusal, NodeTool, off_loop
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
#: A tool result's field holding bytes for the core (a capture), sent as `nox/blob` chunks.
BLOB_KEY = "_blob"
#: Calls running on this node at the same time, at most.
MAX_CONCURRENT_CALLS = 8
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602

#: How `run` ended: stopped locally, removed by the core, or never paired.
Outcome = str
Connector = Callable[..., Awaitable[ClientConnection]]


#: Longest question the dialog shows; anything longer is refused, never cut.
MAX_CONFIRM_CHARS = 1200
#: A "yes" faster than this after the question appeared is a reflex or a stray key, not a
#: decision: it counts as no (review M-c).
MIN_DECISION_S = 1.5
#: Consecutive pings from a core that is not stopped after which a dead-man stop lifts.
DEAD_MAN_RESET_PINGS = 3
#: How often a connected node looks at its certificate's end, and how long it waits for an answer.
RENEW_CHECK_S = 3600.0
RENEW_TIMEOUT_S = 30.0


#: Interface names of virtual adapters (Hyper-V, WSL, Docker, VirtualBox, VMware, bridges):
#: their addresses are listed last, after the real ones, before the cap of 16.
_VIRTUAL_ADAPTERS = ("vethernet", "wsl", "docker", "veth", "br-", "virbr", "hyper-v", "virtualbox",
                     "vmware", "vboxnet", "vmnet", "zt", "tailscale")  # fmt: skip


def local_addresses() -> list[str]:
    """This device's own IP addresses (no loopback), at most 16 - for the core to recognise a
    browser on this PC (`NodeHello.addresses`, every `nox/pong`). Private addresses of real,
    active interfaces first; virtual adapters and interfaces that are down last."""
    import ipaddress  # noqa: PLC0415

    import psutil  # noqa: PLC0415

    try:
        interfaces = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except (OSError, RuntimeError):
        return []
    ranked: list[tuple[int, str]] = []
    for name, entries in interfaces.items():
        up = bool(getattr(stats.get(name), "isup", False))
        virtual = name.lower().startswith(_VIRTUAL_ADAPTERS) or any(
            word in name.lower() for word in ("virtual", "hyper-v", "wsl")
        )
        for entry in entries:
            try:
                ip = ipaddress.ip_address(str(entry.address).split("%", 1)[0])
            except ValueError:
                continue
            if ip.is_loopback:
                continue
            rank = (0 if up else 2) + (1 if virtual else 0) + (0 if ip.is_private else 4)
            ranked.append((rank, str(ip)))
    found: list[str] = []
    for _rank, address in sorted(ranked, key=lambda item: item[0]):
        if address not in found:
            found.append(address)
    return found[:16]


def all_local_addresses() -> list[str]:
    """Every address of every interface of this machine - uncapped and unfiltered, loopback,
    virtual adapters and interfaces that are down included: for telling whether a peer is this
    machine (`NodeControl.controls_local`), where a missing address would fail open."""
    import psutil  # noqa: PLC0415

    try:
        interfaces = psutil.net_if_addrs()
    except (OSError, RuntimeError):
        return []
    found: list[str] = []
    for entries in interfaces.values():
        for entry in entries:
            address = str(entry.address).split("%", 1)[0]
            if address and address not in found:
                found.append(address)
    return found


#: How long the addresses are reused between pongs (they rarely change; a rotating IPv6
#: temporary address is picked up within this).
ADDRESSES_TTL_S = 30.0


def confirmation_text(tool: str, arguments: dict[str, Any], *, tainted: bool = False) -> str | None:
    """The question for the person at the device: every argument in full, one per line, rendered
    exactly as the core's permission target renders it (`protocol.render`: quoted, look-alikes,
    bidi and invisible characters escaped, runs of spaces counted). None when it would be too long
    to read - then nothing is asked and the call is refused (no silent truncation)."""
    try:
        json.dumps(arguments, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, TypeError, ValueError):
        return None  # not valid Unicode (a lone surrogate): refused, not shown escaped
    lines = [f"Nox möchte auf diesem Gerät „{p.escape_controls(tool)}“ ausführen:"]
    for key, value in arguments.items():
        lines.append(f"  {p.escape_controls(str(key))}: {p.render(value)}")
    if tainted:
        lines.append(
            "Achtung: Die Anfrage kam aus einem Gespräch mit fremden Inhalten (Web, Dateien, "
            "Nachrichten). Nur erlauben, wenn du das selbst wolltest."
        )
    lines.append("Erlauben? (Ohne Antwort in 60 s: Ablehnen)")
    text = unicodedata.normalize("NFC", "\n".join(lines))
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None  # not valid Unicode: nothing to show honestly
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
        self._good_pings = 0
        self._dialogs = 0
        self._local_stopped = False
        #: Called whenever the node stops its calls (kill, local stop, connection lost, dead man).
        self.on_stop: Callable[[str], None] = lambda _reason: None
        #: This device's computer-use truth (`p.SessionReport` as a dict), told to the core in the
        #: hello, every pong and every tool answer; None where there is none.
        self.session_report: Callable[[], dict[str, Any] | None] = lambda: None
        #: The core's watched games (`nox/games`): added to this device's list, never removed.
        self.on_games: Callable[[list[str]], None] = lambda _games: None
        #: call id -> (the running call, its JSON-RPC id)
        self._calls: dict[str, tuple[asyncio.Task[None], Any, str]] = {}
        self._last_core = time.monotonic()
        gate.stop_state = self.stopped_reason
        gate.dialog_open = lambda: self._dialogs > 0
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
        #: The core answered that it does not accept this node's protocol (`compatible: false`).
        self.protocol_refused = False
        self.attempts = 0
        #: Answers the node waits for (its own requests to the core), by JSON-RPC id.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._request_seq = 0

    @property
    def busy(self) -> bool:
        """A call runs or a question is open: no restart now."""
        return bool(self._calls) or self._dialogs > 0

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
                "addresses": self._addresses_now(),
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
            # confirmed by nothing less (`nox.node.cli.keep_updated`).
            self.connected.set()
        self._status("connected" if self.compatible else "update_needed", identity)
        log.info("node.connected", compatible=self.compatible)
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
            # The connection is gone: nothing keeps running without the core.
            await self._cancel_calls()

    def _groups(self) -> dict[str, list[str]]:
        """What this device enabled, said outright (the core shows it; it widens nothing): the
        core cannot infer it from the tool list, which leaves out a narrowed group and every tool
        above `max_risk`."""
        return {"groups": sorted(self._gate.manifest.enabled())}

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
            report = self._session_field()
            if isinstance(result, dict) and "content" in result and report:
                # Every tool answer - result, error or refusal - carries the node's truth about
                # computer use, read after the call (the core's lock follows it).
                message["result"] = {
                    **result,
                    "_nox": {**(result.get("_nox") or {}), **report},
                }
        with contextlib.suppress(ConnectionClosed):
            await ws.send(json.dumps(message, ensure_ascii=False))

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

    # -- stopping ------------------------------------------------------------------------------

    def _after_core_ping(self) -> None:
        """The core is back: after `DEAD_MAN_RESET_PINGS` pings in a row while it is not stopped,
        a dead-man stop lifts (the core learns the state from every `nox/pong`)."""
        if not self.dead_man or self.core_killed:
            self._good_pings = 0
            return
        self._good_pings += 1
        if self._good_pings >= DEAD_MAN_RESET_PINGS:
            self.dead_man = False
            self._good_pings = 0
            self._audit_event("node.dead_man_lifted", "core", "pings resumed")

    def stopped_reason(self) -> str | None:
        """Why this node refuses everything right now, or None."""
        if self._local_stopped or self._home.stop_file.exists():
            return "stopped_on_device"
        if self.core_killed:
            return "stopped_by_core"
        if self.dead_man:
            return "dead_man"
        return None

    async def _cancel_input_calls(self, ws: ClientConnection, *, own: str) -> None:
        """Before a question appears, every other running call with a side effect is stopped -
        none may act on the dialog (review M-c)."""
        doomed = {
            key: entry
            for key, entry in self._calls.items()
            if key != own and self._gate.risk_of(entry[2]) != "read"
        }
        for task, _msg_id, _name in doomed.values():
            task.cancel()
        for task, msg_id, name in doomed.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await self._refused(ws, msg_id, name, "dialog_open")

    async def _cancel_calls(self, ws: ClientConnection | None = None) -> list[str]:
        """Cancel every running call; with `ws`, each is answered "stopped". Their ids. A stop,
        a lost connection and the dead-man rule also end what outlives a call (a computer-use
        session, `on_stop`)."""
        try:
            self.on_stop("stopped")
        except Exception as exc:  # noqa: BLE001 - stopping goes on whatever a hook does
            log.error("node.on_stop_failed", error=type(exc).__name__)
        running = dict(self._calls)
        for task, _msg_id, _name in running.values():
            task.cancel()
        for task, _msg_id, _name in running.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if ws is not None:
            for _task, msg_id, _name in running.values():
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

    async def _request(
        self, ws: ClientConnection, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send one request to the core and wait for its answer (TimeoutError after a while)."""
        self._request_seq += 1
        msg_id = f"node-{self._request_seq}"
        waiting: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = waiting
        try:
            await ws.send(
                json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            )
            return await asyncio.wait_for(waiting, RENEW_TIMEOUT_S)
        finally:
            self._pending.pop(msg_id, None)

    async def _renew_loop(self, ws: ClientConnection, identity: NodeIdentity) -> None:
        while True:
            try:
                await self.renew_certificate(ws, identity)
            except (TimeoutError, ConnectionClosed):
                pass  # tried again in an hour, or on the next connection
            except Exception as exc:  # noqa: BLE001 - a failed renewal never ends the connection
                log.warning("node.renew_failed", error=type(exc).__name__)
            await asyncio.sleep(RENEW_CHECK_S)

    async def renew_certificate(self, ws: ClientConnection, identity: NodeIdentity) -> bool:
        """Ask for a new certificate once the current one is in its last days (spec C Â§1.2).
        True when a new one is stored; it is used from the next connection on."""
        if not due(self._home.cert_file):
            return False
        key = self._keystore.load()
        answer = await self._request(ws, p.M_RENEW, {"csr": self._keystore.csr(key, identity.name)})
        result = answer.get("result")
        cert_pem = result.get("cert") if isinstance(result, dict) else None
        if not isinstance(cert_pem, str):
            log.warning("node.renew_refused", reason=str(answer.get("error"))[:100])
            return False
        try:
            accept_renewed(
                cert_pem,
                ca_file=self._home.ca_file,
                current_file=self._home.cert_file,
                key=key,
                node_id=identity.node_id,
            )
        except RenewalRefusedError as exc:
            log.error("node.renew_bad_certificate", reason=str(exc))
            self._audit_event("node.cert_renewed", "core", f"refused: {exc}")
            return False
        store_certificate(self._home.cert_file, cert_pem)
        self._audit_event("node.cert_renewed", "core", "ok")
        log.info("node.certificate_renewed")
        return True

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

    def _audit_refusal(self, tool: str, call_id: str, reason: str) -> None:
        if self._audit is not None:
            self._audit.append(
                actor="local",
                tool=p.safe_name(tool),
                action="node.refused",
                target=p.safe_name(call_id, 64),
                decision="deny",
                result=reason,
            )

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
        # One text for everything: the receipt's hash, the dialog and the handler all see the
        # same NFC form of the arguments (review: never sign one and run another).
        arguments = normalize_arguments(params.get("arguments"))
        # The slow text check first, off the event loop and stateless: pings, the stop and the
        # dead man keep running, and a check that runs out of time is simply abandoned - it
        # never spends budget, consumes a receipt or records an "allow".
        try:
            early = await off_loop(self._gate.precheck, name, arguments)
        except TimeoutError:
            early = "check_timeout"
        if early is not None:
            self._audit_refusal(name, meta.call_id, p.safe_name(early, 64))
            await self._refused(ws, msg_id, name, early)
            return
        decision = self._gate.check(
            name, arguments, call_id=meta.call_id, meta=meta, prechecked=True
        )
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
            with contextlib.suppress(Exception):
                self.on_stop("dead_man")
            self._audit_event("node.dead_man", "local", p.safe_name(name))
            await self._refused(ws, msg_id, name, "dead_man")
            return
        except NodeRefusal as exc:
            # The hand's own boundary (outside the allowed folders, no sandbox): a refusal on
            # this device, audited like the gate's.
            reason = p.safe_name(exc.reason, 64)
            self._audit_refusal(name, meta.call_id, reason)
            await self._refused(ws, msg_id, name, reason, detail=exc.detail)
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
        blob = result.pop(BLOB_KEY, None) if isinstance(result, dict) else None
        if isinstance(blob, bytes):
            # A capture: its bytes go to the core that asked, in chunks, and are dropped here
            # with this call - the node keeps no copy (spec C §1.4, C6).
            sent = await self._send_blob(ws, meta.call_id, blob)
            del blob
            if not sent:
                await self._reply(
                    ws,
                    msg_id,
                    result={
                        "isError": True,
                        "content": [{"type": "text", "text": "capture too large"}],
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

    def _addresses_now(self) -> list[str]:
        """`local_addresses`, reused for `ADDRESSES_TTL_S`."""
        now = time.monotonic()
        cached = getattr(self, "_addresses_cache", None)
        if cached is None or now - cached[0] > ADDRESSES_TTL_S:
            cached = (now, local_addresses())
            self._addresses_cache = cached
        return list(cached[1])

    def _session_field(self) -> dict[str, Any]:
        try:
            report = self.session_report()
        except Exception as exc:  # noqa: BLE001 - no report rather than no answer
            log.warning("node.session_report_failed", error=type(exc).__name__)
            return {}
        return {"session": report} if report is not None else {}

    async def _send_blob(self, ws: ClientConnection, call_id: str, data: bytes) -> bool:
        """`nox/blob` chunks of one result, before its answer; False when it is too large."""
        if not data or len(data) > p.MAX_RESULT_BYTES:
            return False
        size = p.BLOB_CHUNK_BYTES
        pieces = [data[i : i + size] for i in range(0, len(data), size)]
        for seq, piece in enumerate(pieces):
            params = {
                "call_id": call_id,
                "seq": seq,
                "last": seq == len(pieces) - 1,
                "data": base64.b64encode(piece).decode("ascii"),
            }
            with contextlib.suppress(ConnectionClosed):
                await ws.send(json.dumps({"jsonrpc": "2.0", "method": p.M_BLOB, "params": params}))
        return True

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
            # `trust_core_for_high` trusts a confirmation a PERSON gave on the core. A call the
            # core left to this device, or allowed by a rule or a remembered answer, was
            # confirmed by nobody for this call: refuse it (review M-b).
            trusted = self._gate.manifest.trust_core_for_high
            if trusted and not meta.confirm_on_device and meta.decided_by == "user":
                self._audit_event("node.confirm", "core", "trusted (no screen)")
                return None
            return "high_needs_local_confirmation"
        shown = tool.shown(args) if tool.shown is not None else args.model_dump()
        text = confirmation_text(tool.name, shown, tainted=meta.tainted)
        if text is None:
            # Never ask about something the person cannot see in full.
            self._audit_event("node.confirm", "local", f"{p.safe_name(tool.name)}:cannot_show")
            return "cannot_show_in_full"
        self._dialogs += 1  # from here on no new input call starts
        await self._cancel_input_calls(ws, own=meta.call_id)
        shown_at = time.monotonic()
        try:
            answer = await self._confirmer.ask(text)
        except Exception as exc:  # noqa: BLE001 - a broken dialog is a "no", never a "yes"
            log.warning("node.confirm_failed", error=type(exc).__name__)
            answer = "deny"
        finally:
            self._dialogs -= 1
        if answer == "allow" and time.monotonic() - shown_at < MIN_DECISION_S:
            answer = "deny"  # too fast to have been read: a reflex or a stray key
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

    async def _refused(
        self, ws: ClientConnection, msg_id: Any, name: str, reason: str, *, detail: str = ""
    ) -> None:
        log.warning("node.call_refused", tool=p.safe_name(name, 64), reason=reason)
        text = f"refused on the device: {reason}"
        if detail:
            text += f" ({p.escape_controls(detail)[:200]})"
        await self._reply(
            ws,
            msg_id,
            result={
                "isError": True,
                "_nox": {"refused": reason},
                "content": [{"type": "text", "text": text}],
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
