"""The layer of `NodeAgent` for what the node asks or tells the core outside a tool call, and for
the renewal of its certificate."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from nox.core.logging import get_logger
from nox.node.device.agent.calls import CallRunner
from nox.node.device.home import NodeIdentity
from nox.node.device.renewal import RenewalRefusedError, accept_renewed, due, store_certificate
from nox.node.wire import protocol as p

log = get_logger(__name__)


#: How often a connected node looks at its certificate's end, and how long it waits for an answer.
RENEW_CHECK_S = 3600.0


RENEW_TIMEOUT_S = 30.0


class CoreRequests(CallRunner):
    """What the node asks or tells the core outside a tool call (`ask_core`, `notify_core`) and the
    renewal of its certificate."""

    async def notify_core(self, method: str, params: dict[str, Any]) -> bool:
        """A satellite's notification to the core - only one the core said it understands, on
        the current connection; False when there is none."""
        ws = self._ws
        if ws is None or method not in p.NODE_TO_CORE or not self._understood(method):
            return False
        try:
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))
        except ConnectionClosed:
            return False
        return True

    async def ask_core(
        self, method: str, params: dict[str, Any], *, timeout_s: float = 10.0
    ) -> dict[str, Any] | None:
        """A satellite's request to the core and its answer; None without a connection, for a
        method the core did not say it understands, or without an answer in time."""
        ws = self._ws
        if ws is None or method not in p.NODE_TO_CORE or not self._understood(method):
            return None
        try:
            return await self._request(ws, method, params, timeout_s=timeout_s)
        except (TimeoutError, ConnectionClosed, asyncio.CancelledError):
            return None

    def _understood(self, method: str) -> bool:
        needs = {
            p.M_VOICE_HEARD: p.FEATURE_VOICE,
            p.M_VOICE_LEVEL: p.FEATURE_VOICE,
            p.M_OVERLAY_CREDENTIAL: p.FEATURE_OVERLAY,
        }.get(method)
        return needs is None or needs in self.features

    async def _request(
        self,
        ws: ClientConnection,
        method: str,
        params: dict[str, Any],
        *,
        timeout_s: float = RENEW_TIMEOUT_S,
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
            return await asyncio.wait_for(waiting, timeout_s)
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
        """Ask for a new certificate once the current one is in its last days.
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
