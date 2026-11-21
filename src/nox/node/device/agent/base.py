"""The bottom layer of `NodeAgent`: its state and the helpers every other layer uses."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from nox.core.logging import get_logger
from nox.node.device.confirm import Confirmer, PlatformConfirmer
from nox.node.device.gate import NodeGate
from nox.node.device.home import NodeHome
from nox.node.device.keystore import NodeKeyStore
from nox.node.wire import protocol as p
from nox.node.wire.manifest import NodeManifest
from nox.security.model import AuditLog

log = get_logger(__name__)


PING_INTERVAL_S = 1.0


ERR_METHOD_NOT_FOUND = -32601


ERR_INVALID_PARAMS = -32602


Connector = Callable[..., Awaitable[ClientConnection]]


class AgentBase:
    """The state of a node's connection to its core and the helpers every part of the agent
    uses: the stop state, a reply on the wire and an audit line.

    `NodeAgent` is this class plus the layers above it (see the package docstring)."""

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
        #: What the connected core said it understands (`features`); empty while not connected.
        self.features: frozenset[str] = frozenset()
        #: The satellites' handlers for what the core sends them (`p.SATELLITE_TO_NODE`): each
        #: validates its own parameters and returns at once (long work goes into a task).
        self.satellites: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        #: Told whenever the connection to a compatible core comes (True) or goes (False).
        self.on_connection: list[Callable[[bool], None]] = []
        self._ws: ClientConnection | None = None

    @property
    def busy(self) -> bool:
        """A call runs or a question is open: no restart now."""
        return bool(self._calls) or self._dialogs > 0

    def stopped_reason(self) -> str | None:
        """Why this node refuses everything right now, or None."""
        if self._local_stopped or self._home.stop_file.exists():
            return "stopped_on_device"
        if self.core_killed:
            return "stopped_by_core"
        if self.dead_man:
            return "dead_man"
        return None

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

    def _session_field(self) -> dict[str, Any]:
        try:
            report = self.session_report()
        except Exception as exc:  # noqa: BLE001 - no report rather than no answer
            log.warning("node.session_report_failed", error=type(exc).__name__)
            return {}
        return {"session": report} if report is not None else {}

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
