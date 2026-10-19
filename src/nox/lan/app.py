"""The network listener: the dashboard over HTTPS for paired devices in the home network.

What it serves, and nothing else:
  * `/dashboard/` - the same built dashboard the PC uses,
  * `POST /api/pair` - a new device redeems a one-time code for its own token,
  * `GET /api/lan/info` - the authority's fingerprint and this machine's addresses (public facts,
    shown on the pairing screen so a person can compare them),
  * `GET /nox-ca.crt` - the authority itself, for devices that want no browser warning,
  * `/ws` - the device's WebSocket, bridged to the loopback hub.

What it never serves: the per-start session token, `/api/state`, `/api/providers` or the pet. A
device authenticates on the hub with its own token, as role `device`, and the hub accepts nothing
else on a bridged connection (`nox.ipc.server.LAN_BRIDGE_HEADER`).

Browser hardening, on every response:
  * **Host check.** The Host header must be one of the names and addresses the certificate was
    issued for. A web page elsewhere that points its own domain at this machine (DNS rebinding)
    is turned away before any route runs.
  * **Origin check** on `POST /api/pair` and the WebSocket: only this listener's own origin. A web
    page in another tab of the same phone cannot pair or connect in the person's name.
  * A strict Content-Security-Policy (scripts, frames and connections from this origin only),
    `frame-ancestors 'none'`, `no-store`, `no-referrer`, `nosniff`.
  * Pairing attempts are limited per source address and in total, and a wrong code costs one.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from nox.core.logging import get_logger
from nox.ipc.server import LAN_BRIDGE_HEADER, LAN_PEER_HEADER, WS_PATH
from nox.lan.devices import CLIENT_PREFIX, LanDevices
from nox.lan.tls import TlsMaterial

log = get_logger(__name__)

__all__ = ["LanSettings", "PairLimiter", "create_lan_app", "host_of"]

CA_FILE_NAME = "nox-home-ca.crt"
CLOSE_POLICY_VIOLATION = 1008
CLOSE_INTERNAL_ERROR = 1011
MAX_FRAME_BYTES = 1024 * 1024

_BASE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def _csp(host: str) -> str:
    # `connect-src 'self'` covers wss:// to the same host in current browsers; the explicit
    # wss:// origin is for the ones that still read 'self' as http(s) only.
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            f"connect-src 'self' wss://{host}",
            "manifest-src 'self'",
            "worker-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


class LanSettings(BaseModel):
    """Where the network listener binds. Any address - binding the home network is its job."""

    host: str = "0.0.0.0"  # noqa: S104 - see the class docstring
    port: int = Field(default=47843, ge=0, le=65535)
    port_attempts: int = Field(default=1, ge=1)


def _port_of(header: str) -> int | None:
    """The port of a Host header, or None when there is none or it is not a plain number."""
    value = header.strip()
    tail = value.rsplit("]", 1)[1] if value.startswith("[") else value
    if ":" not in tail:
        return None
    digits = tail.rsplit(":", 1)[1]
    return int(digits) if digits.isdigit() and 0 < int(digits) < 65536 else -1


def host_of(header: str) -> str:
    """The host part of a Host header: no port, no IPv6 brackets, lower case."""
    value = header.strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def _same_origin(headers: Headers) -> bool:
    origin = headers.get("origin", "")
    host = headers.get("host", "")
    return bool(origin) and bool(host) and origin.lower() == f"https://{host.lower()}"


class PairLimiter:
    """Sliding one-minute window, per source address and for all of them together.

    The total cap matters because a network can offer many source addresses; with 40-bit codes
    valid for five minutes it keeps guessing hopeless either way.
    """

    def __init__(
        self,
        *,
        per_address: int,
        total: int = 30,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_address = per_address
        self._total = total
        self._window = window_s
        self._clock = clock
        self._by_address: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _trim(self, hits: deque[float], now: float) -> None:
        while hits and hits[0] <= now - self._window:
            hits.popleft()

    def allow(self, address: str) -> bool:
        now = self._clock()
        self._trim(self._all, now)
        for key in list(self._by_address):
            self._trim(self._by_address[key], now)
            # Forget quiet addresses, so the map cannot grow with every address ever seen.
            if not self._by_address[key]:
                del self._by_address[key]
        hits = self._by_address.setdefault(address, deque())
        if len(hits) >= self._per_address or len(self._all) >= self._total:
            return False
        hits.append(now)
        self._all.append(now)
        return True


class PairBody(BaseModel):
    code: str = Field(min_length=4, max_length=16)
    name: str = Field(default="", max_length=64)


class _Guard:
    """Pure-ASGI middleware: Host check and security headers for HTTP and WebSocket alike."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: Callable[[], Iterable[str]]) -> None:
        self._app = app
        self._allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host_header = headers.get("host", "")
        host, port = host_of(host_header), _port_of(host_header)
        if host not in set(self._allowed_hosts()) or port == -1:
            log.warning("lan.host_refused", host=host_header[:80])
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": CLOSE_POLICY_VIOLATION})
                return
            response = Response("unknown host\n", status_code=421, headers=_BASE_HEADERS)
            await response(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                for key, value in _BASE_HEADERS.items():
                    out[key] = value
                # Only the validated host and a numeric port reach the policy, never the raw header.
                shown = f"[{host}]" if ":" in host else host
                out["Content-Security-Policy"] = _csp(shown if port is None else f"{shown}:{port}")
            await send(message)

        await self._app(scope, receive, send_with_headers)


def _client_address(scope_client: Any) -> str:
    if not scope_client:
        return ""
    host = str(scope_client[0])
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host[:64]


def create_lan_app(
    *,
    devices: LanDevices,
    tls: Callable[[], TlsMaterial],
    hub_url: Callable[[], str],
    bridge_secret: Callable[[], str],
    dashboard_dist: Path | None,
    limiter: PairLimiter,
) -> Starlette:
    """Build the listener's app. Everything is injected, so tests run it without a core."""

    def allowed_hosts() -> list[str]:
        material = tls()
        return [*material.names, *material.addresses]

    async def root(_request: Request) -> Response:
        return RedirectResponse("/dashboard/", status_code=302)

    async def info(_request: Request) -> Response:
        material = tls()
        return JSONResponse(
            {
                "fingerprint": material.fingerprint,
                "names": [n for n in material.names if n != "localhost"],
                "addresses": [a for a in material.addresses if a != "127.0.0.1"],
                "ca_url": "/nox-ca.crt",
            }
        )

    async def ca_certificate(_request: Request) -> Response:
        return Response(
            tls().ca_der,
            media_type="application/x-x509-ca-cert",
            headers={"Content-Disposition": f'attachment; filename="{CA_FILE_NAME}"'},
        )

    async def pair(request: Request) -> Response:
        address = _client_address(request.client)
        if not _same_origin(request.headers):
            log.warning("lan.pair_origin_refused", peer=address)
            return JSONResponse({"error": "origin"}, status_code=403)
        if not limiter.allow(address):
            log.warning("lan.pair_rate_limited", peer=address)
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        try:
            body = PairBody.model_validate(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse({"error": "invalid"}, status_code=400)
        redeemed = await devices.redeem(body.code, name=body.name)
        result = redeemed.result
        if not result.ok or redeemed.token is None:
            status = 409 if result.reason == "max_devices" else 401
            return JSONResponse({"error": result.reason}, status_code=status)
        log.info("lan.device_paired", device_id=result.device_id, peer=address)
        return JSONResponse(
            {
                "device_id": result.device_id,
                "client_id": f"{CLIENT_PREFIX}{result.device_id}",
                "name": result.name,
                "token": redeemed.token,
            },
            status_code=201,
        )

    async def bridge(websocket: WebSocket) -> None:
        peer = _client_address(websocket.client)
        if not _same_origin(websocket.headers):
            log.warning("lan.ws_origin_refused", peer=peer)
            await websocket.close(code=CLOSE_POLICY_VIOLATION)
            return
        headers = {LAN_BRIDGE_HEADER: bridge_secret(), LAN_PEER_HEADER: peer}
        try:
            upstream = await connect(
                hub_url(),
                additional_headers=headers,
                max_size=MAX_FRAME_BYTES,
                compression=None,
                open_timeout=5,
                ping_interval=None,
            )
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            log.warning("lan.hub_unreachable", error=f"{type(exc).__name__}: {exc}")
            await websocket.close(code=CLOSE_INTERNAL_ERROR)
            return
        await websocket.accept()
        close_code = 1000

        async def device_to_hub() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])

        async def hub_to_device() -> None:
            nonlocal close_code
            try:
                async for frame in upstream:
                    if isinstance(frame, str):
                        await websocket.send_text(frame)
                    else:
                        await websocket.send_bytes(frame)
            except ConnectionClosed:
                pass
            close_code = upstream.close_code or 1000

        tasks = [
            asyncio.create_task(device_to_hub(), name="lan-up"),
            asyncio.create_task(hub_to_device(), name="lan-down"),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                failure = task.exception()
                if failure is not None and not isinstance(
                    failure, (WebSocketDisconnect, ConnectionClosed)
                ):
                    log.warning("lan.bridge_error", error=f"{type(failure).__name__}: {failure}")
        finally:
            await upstream.close()
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                # The hub's code reaches the device: 1008 after a revocation, so its dashboard
                # knows it was removed rather than merely disconnected.
                await websocket.close(code=close_code)

    routes: list[Route | Mount | WebSocketRoute] = [
        Route("/", root, methods=["GET"]),
        Route("/api/lan/info", info, methods=["GET"]),
        Route("/api/pair", pair, methods=["POST"]),
        Route("/nox-ca.crt", ca_certificate, methods=["GET"]),
        WebSocketRoute(WS_PATH, bridge),
    ]
    if dashboard_dist is not None and (dashboard_dist / "index.html").is_file():
        routes.append(
            Mount("/dashboard", app=StaticFiles(directory=str(dashboard_dist), html=True))
        )
    else:

        async def not_built(_request: Request) -> Response:
            return Response("Nox dashboard is not built.\n", status_code=503)

        routes.append(Route("/dashboard/{rest:path}", not_built, methods=["GET"]))
    app = Starlette(routes=routes)
    app.add_middleware(_Guard, allowed_hosts=allowed_hosts)
    return app
