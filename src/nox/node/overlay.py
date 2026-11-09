"""The overlay relay (C9): the pet as an OBS browser source on a PC that is not the core.

`nox node` serves, on this device's loopback only (`http://127.0.0.1:47845`), the pet page in
overlay mode and one WebSocket, both relayed to the core's network listener over TLS against the
authority this node pinned at pairing - never another one:

    OBS browser source (or a browser on this PC)
      -> http://127.0.0.1:47845/pet/?overlay=1&device=<this node>#overlay=relay
      -> this relay -> wss://<core>:47843/ws (overlay role, read-only)

* **The credential never sits in OBS.** The URL carries the placeholder `relay`; the relay
  swaps in a read-only overlay credential the core issued to this node (`nox/overlay/credential`:
  role `overlay`, label `node:<id>`, usable through the network listener only for that label).
  It lives in this process's memory, a new one replaces it on every connection to the core (the
  old one is revoked and its pages disconnected: rotation), and it goes when the node is removed
  or the core switches `obs` off for it. Never the session token, never an admin token: the
  overlay role may watch the pet's events and read the public state snapshot, nothing else.
* **Only the overlay.** The first frame must be an `ipc.auth` for role `overlay`; anything else
  is refused. Only loopback Host headers are answered (no DNS rebinding) and a WebSocket only from
  this relay's own origin or none (OBS), never from a web page elsewhere.
* **Offline**: no core, no credential, or the group `obs` off here - the page gets its WebSocket
  closed and shows the pet as offline; it reconnects by itself (`ui/shared/ipc.ts`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
import ssl
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.typing import Origin

from nox.core.logging import get_logger
from nox.node.home import NodeIdentity

log = get_logger(__name__)

__all__ = ["PLACEHOLDER", "OverlayRelay", "OverlaySettings", "overlay_url"]

#: What the overlay URL carries instead of a credential.
PLACEHOLDER = "relay"
DEFAULT_PORT = 47845
CLOSE_POLICY_VIOLATION = 1008
CLOSE_TRY_AGAIN_LATER = 1013
FIRST_FRAME_TIMEOUT_S = 10.0
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_FRAME_BYTES = 1024 * 1024
#: Connections at once: one OBS scene rarely has more than a few overlays.
MAX_CONNECTIONS = 8
_ASSET_PATH = re.compile(r"^[A-Za-z0-9_./-]{0,200}$")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self' ws://127.0.0.1:{port} "
    "ws://localhost:{port}; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


class OverlaySettings(BaseModel):
    """The `overlay:` part of this node's `satellites.yaml`."""

    model_config = ConfigDict(extra="forbid")

    port: int = Field(default=DEFAULT_PORT, ge=1024, le=65535)


def overlay_url(port: int, device_id: str, *, variant: str = "") -> str:
    """The URL to put into an OBS browser source on this PC: no secret in it."""
    query = "overlay=1" + (f"&variant={variant}" if variant else "") + f"&device={device_id}"
    return f"http://127.0.0.1:{port}/pet/?{query}#overlay={PLACEHOLDER}"


#: Fetches one file from the core's network listener: (status, content type, body).
Fetch = Callable[[str], Awaitable[tuple[int, str, bytes]]]
Connect = Callable[..., Awaitable[Any]]


def pinned_context(ca_file: Path) -> ssl.SSLContext:
    """TLS to the core: only the authority pinned at pairing, host name checked."""
    context = ssl.create_default_context(cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _authority(identity: NodeIdentity) -> str:
    host = f"[{identity.core_host}]" if ":" in identity.core_host else identity.core_host
    return f"{host}:{identity.core_port}"


def local_host(header: str, port: int) -> bool:
    return header.strip().lower() in (f"127.0.0.1:{port}", f"localhost:{port}")


def local_origin(origin: str | None, port: int) -> bool:
    """No Origin (OBS, a program) or this relay's own; never another site's."""
    if origin is None or origin == "":
        return True
    return origin.lower() in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def swap_token(frame: str, token: str) -> str | None:
    """The page's `ipc.auth` with the node's credential in place of the placeholder - or None
    for anything but an overlay's authentication."""
    try:
        envelope = json.loads(frame)
    except (ValueError, RecursionError):
        return None
    if not isinstance(envelope, dict) or envelope.get("name") != "ipc.auth":
        return None
    payload, source = envelope.get("payload"), envelope.get("src")
    if not isinstance(payload, dict) or not isinstance(source, dict):
        return None
    if payload.get("role") != "overlay" or source.get("role") != "overlay":
        return None
    envelope["payload"] = {**payload, "token": token}
    return json.dumps(envelope, ensure_ascii=False)


@dataclass
class OverlayRelay:
    identity: Callable[[], NodeIdentity | None]
    ca_file: Path
    #: The group `obs` is enabled here (and not narrowed), the node is connected and not stopped.
    allowed: Callable[[], bool]
    #: A credential for this node from the core (None: refused or not connected).
    credential: Callable[[], Awaitable[str | None]]
    settings: Callable[[], OverlaySettings] = OverlaySettings
    fetch: Fetch | None = None
    connect: Connect | None = None
    _token: str | None = None
    _open: set[WebSocket] = field(default_factory=set)
    _server: Any = None

    @property
    def port(self) -> int:
        return self.settings().port

    def forget_credential(self) -> None:
        """The connection to the core changed: the next page asks for a new credential."""
        self._token = None

    async def _credential(self) -> str | None:
        if self._token is None:
            self._token = await self.credential()
        return self._token

    async def close_all(self, reason: str = "overlay off") -> None:
        for websocket in list(self._open):
            with contextlib.suppress(Exception):
                await websocket.close(code=CLOSE_TRY_AGAIN_LATER, reason=reason[:60])

    # -- HTTP ------------------------------------------------------------------------------------

    async def _fetch(self, path: str) -> tuple[int, str, bytes]:
        if self.fetch is not None:
            return await self.fetch(path)
        identity = self.identity()
        if identity is None:
            return 503, "text/plain", b"not paired\n"
        import httpx  # noqa: PLC0415

        url = f"https://{_authority(identity)}{path}"
        async with httpx.AsyncClient(verify=pinned_context(self.ca_file), timeout=10) as client:
            response = await client.get(url)
            body = response.content[: MAX_ASSET_BYTES + 1]
            return response.status_code, response.headers.get("content-type", ""), body

    def app(self) -> Starlette:
        port = self.port

        def headers() -> dict[str, str]:
            return {
                "Content-Security-Policy": _CSP.format(port=port),
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            }

        async def health(request: Request) -> Response:
            if not local_host(request.headers.get("host", ""), port):
                return Response("not here\n", status_code=403)
            return JSONResponse({"ws_port": port, "relay": True}, headers=headers())

        async def asset(request: Request) -> Response:
            if not local_host(request.headers.get("host", ""), port):
                return Response("not here\n", status_code=403)
            rest = str(request.path_params.get("rest", ""))
            if not _ASSET_PATH.fullmatch(rest) or ".." in rest.split("/"):
                return Response("not found\n", status_code=404)
            try:
                status, kind, body = await self._fetch(f"/pet/{rest}")
            except Exception as exc:  # noqa: BLE001 - the core is not reachable: say so
                log.info("node.overlay_asset_failed", error=type(exc).__name__)
                return Response("Nox is not reachable\n", status_code=502, headers=headers())
            if len(body) > MAX_ASSET_BYTES:
                return Response("too large\n", status_code=502, headers=headers())
            return Response(body, status_code=status, media_type=kind or None, headers=headers())

        return Starlette(
            routes=[
                Route("/health", health, methods=["GET"]),
                Route("/pet/{rest:path}", asset, methods=["GET"]),
                WebSocketRoute("/ws", self._bridge),
            ]
        )

    # -- the WebSocket ---------------------------------------------------------------------------

    async def _bridge(self, websocket: WebSocket) -> None:
        port = self.port
        if not local_host(websocket.headers.get("host", ""), port) or not local_origin(
            websocket.headers.get("origin"), port
        ):
            log.warning("node.overlay_refused", reason="host_or_origin")
            await websocket.close(code=CLOSE_POLICY_VIOLATION)
            return
        if len(self._open) >= MAX_CONNECTIONS or not self.allowed():
            await websocket.close(code=CLOSE_TRY_AGAIN_LATER)
            return
        await websocket.accept()
        self._open.add(websocket)
        try:
            await self._relay(websocket)
        finally:
            self._open.discard(websocket)

    async def _relay(self, websocket: WebSocket) -> None:
        try:
            first = await asyncio.wait_for(websocket.receive_text(), FIRST_FRAME_TIMEOUT_S)
        except (TimeoutError, WebSocketDisconnect, KeyError, RuntimeError):
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=CLOSE_POLICY_VIOLATION)
            return
        if swap_token(first, PLACEHOLDER) is None:
            # Not an overlay's authentication: this relay carries nothing else.
            log.warning("node.overlay_refused", reason="not_an_overlay")
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=CLOSE_POLICY_VIOLATION)
            return
        identity = self.identity()
        token = await self._credential() if identity is not None and self.allowed() else None
        frame = swap_token(first, token) if token else None
        if identity is None or frame is None:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=CLOSE_TRY_AGAIN_LATER)  # offline: the page retries
            return
        authority = _authority(identity)
        connect = self.connect
        if connect is None:
            from websockets.asyncio.client import connect as ws_connect  # noqa: PLC0415

            connect = ws_connect
        try:
            upstream = await connect(
                f"wss://{authority}/ws",
                ssl=pinned_context(self.ca_file),
                server_hostname=identity.core_host,
                origin=Origin(f"https://{authority}"),
                max_size=MAX_FRAME_BYTES,
                compression=None,
                open_timeout=5,
                ping_interval=None,
            )
        except Exception as exc:  # noqa: BLE001 - offline: the page shows it and retries
            log.info("node.overlay_upstream_failed", error=type(exc).__name__)
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=CLOSE_TRY_AGAIN_LATER)
            return
        try:
            await upstream.send(frame)
            await self._pump(websocket, upstream)
        finally:
            with contextlib.suppress(Exception):
                await upstream.close()
            if getattr(upstream, "close_code", None) == CLOSE_POLICY_VIOLATION:
                self.forget_credential()  # refused or revoked: the next page asks for a new one
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()

    async def _pump(self, websocket: WebSocket, upstream: Any) -> None:
        async def down() -> None:
            async for message in upstream:
                if isinstance(message, str):
                    await websocket.send_text(message)

        async def up() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                text = message.get("text")
                if isinstance(text, str):
                    if not self.allowed():
                        return  # switched off meanwhile
                    await upstream.send(text)

        tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- serving ---------------------------------------------------------------------------------

    def bind(self) -> socket.socket:
        """The relay's socket on loopback (OSError when the port is taken). Bound here, not by
        uvicorn, which would end the whole process on a taken port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", self.port))
            sock.listen(16)
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        return sock

    async def serve(self, stop: asyncio.Event) -> None:
        """Serve on loopback until `stop` (only while `obs` is enabled here: the caller decides).
        Raises OSError when the port is taken."""
        import uvicorn  # noqa: PLC0415

        sock = self.bind()
        config = uvicorn.Config(
            self.app(),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="off",
            access_log=False,
            ws_max_size=MAX_FRAME_BYTES,
        )
        server = uvicorn.Server(config)
        self._server = server
        task = asyncio.create_task(self._quietly(server.serve(sockets=[sock])))
        try:
            await stop.wait()
        finally:
            await self.close_all("node stopping")
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, 5)
            sock.close()
            self._server = None

    @staticmethod
    async def _quietly(work: Awaitable[None]) -> None:
        """uvicorn ends a failed start with `SystemExit`: never let that leave the relay."""
        try:
            await work
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            log.warning("node.overlay_server_ended", error=type(exc).__name__)
