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
  * Only loopback and private networks are answered, unless `allow_public_clients` says otherwise:
    a port forward or a public address on the machine does not put the pairing page on the
    internet.
  * Connections that have not authenticated yet are capped in total and per address, the hub is
    only dialled once the device sent its first frame, and a pairing body is at most 1 KB.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import json
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from nox.core.logging import get_logger
from nox.ipc.server import LAN_BRIDGE_HEADER, LAN_PEER_HEADER, WS_PATH
from nox.lan.devices import CLIENT_PREFIX, LanDevices
from nox.lan.tls import TlsMaterial, permitted_address

log = get_logger(__name__)

__all__ = ["LanSettings", "PairLimiter", "create_lan_app", "host_of"]

CA_FILE_NAME = "nox-home-ca.crt"
CLOSE_POLICY_VIOLATION = 1008
CLOSE_TRY_AGAIN_LATER = 1013
CLOSE_INTERNAL_ERROR = 1011
MAX_FRAME_BYTES = 1024 * 1024
MAX_PAIR_BODY_BYTES = 1024
#: WebSockets that have not authenticated yet, in total and per source address.
MAX_PREAUTH_TOTAL = 16
MAX_PREAUTH_PER_ADDRESS = 4
#: How long a new WebSocket may take to send its first (auth) frame.
FIRST_FRAME_TIMEOUT_S = 5.0

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
    """The port of a Host header: None when there is none, -1 when it is not a valid port."""
    value = header.strip()
    tail = value.rsplit("]", 1)[1] if value.startswith("[") else value
    if ":" not in tail:
        return None
    digits = tail.rsplit(":", 1)[1]
    return int(digits) if digits.isdigit() and 0 < int(digits) < 65536 else -1


def host_of(header: str) -> str:
    """The host part of a Host header: no port, no IPv6 brackets, lower case. A bracketed host
    followed by anything but `:port` is no host at all."""
    value = header.strip().lower()
    if value.startswith("["):
        inside, _, rest = value[1:].partition("]")
        return inside if rest == "" or rest.startswith(":") else ""
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
    #: Typed on the new device; empty means "use the name chosen on the PC, or my guess".
    name: str = Field(default="", max_length=64)
    #: What the device guesses it is ("iPhone"), used when nobody chose a name.
    default_name: str = Field(default="", max_length=64)


def _refusal(title: str, links: list[str]) -> str:
    """A small page in both languages instead of a bare "unknown host": a household member who
    typed an old address learns the right one."""
    items = "".join(f'<li><a href="{html.escape(u)}">{html.escape(u)}</a></li>' for u in links)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Nox</title></head><body style='font-family:system-ui,sans-serif;margin:2rem;"
        f"max-width:36rem;line-height:1.5'><h1>{html.escape(title)}</h1>"
        "<p>Nox antwortet unter dieser Adresse nicht. Öffne Nox über eine dieser Adressen:</p>"
        "<p>Nox does not answer at this address. Open it at one of these:</p>"
        f"<ul>{items}</ul></body></html>"
    )


class _Guard:
    """Pure-ASGI middleware: peer and Host checks and security headers, HTTP and WebSocket."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Callable[[], Iterable[str]],
        allow_public_clients: bool = False,
    ) -> None:
        self._app = app
        self._allowed_hosts = allowed_hosts
        self._allow_public = allow_public_clients

    def _links(self, scope: Scope) -> list[str]:
        server = scope.get("server") or ("", 0)
        port = int(server[1] or 0)
        out = []
        for host in self._allowed_hosts():
            if host in ("localhost", "127.0.0.1"):
                continue
            shown = f"[{host}]" if ":" in host else host
            out.append(f"https://{shown}:{port}/dashboard/")
        return out

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        peer = _client_address(scope.get("client"))
        if not self._allow_public and not permitted_address(peer):
            log.warning("lan.peer_refused", peer=peer)
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": CLOSE_POLICY_VIOLATION})
                return
            response = Response("forbidden\n", status_code=403, headers=_BASE_HEADERS)
            await response(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host_header = headers.get("host", "")
        host, port = host_of(host_header), _port_of(host_header)
        if not host or host not in set(self._allowed_hosts()) or port == -1:
            log.warning("lan.host_refused", host=host_header[:80])
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": CLOSE_POLICY_VIOLATION})
                return
            page = _refusal("Nox", self._links(scope))
            response = HTMLResponse(page, status_code=421, headers=_BASE_HEADERS)
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


def _close_code(code: int | None) -> int:
    """A close code the browser may receive. 1005/1006/1015 only describe what happened on the
    wire and must never be sent; anything outside the sendable ranges becomes 1011."""
    if code is None:
        return 1000
    if 1000 <= code <= 1003 or 1007 <= code <= 1014 or 3000 <= code <= 4999:
        return code
    return CLOSE_INTERNAL_ERROR


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
    allow_public_clients: bool = False,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> Starlette:
    """Build the listener's app. Everything is injected, so tests run it without a core."""
    preauth: dict[str, int] = {}

    def allowed_hosts() -> list[str]:
        material = tls()
        return [*material.names, *material.addresses]

    async def root(_request: Request) -> Response:
        return RedirectResponse("/dashboard/", status_code=302)

    async def info(_request: Request) -> Response:
        material = tls()
        return JSONResponse(
            {
                "fingerprint": material.server_fingerprint,
                "ca_fingerprint": material.fingerprint,
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
        declared = request.headers.get("content-length", "")
        if declared and (not declared.isdigit() or int(declared) > MAX_PAIR_BODY_BYTES):
            return JSONResponse({"error": "invalid"}, status_code=413)
        raw = await request.body()
        if len(raw) > MAX_PAIR_BODY_BYTES:
            return JSONResponse({"error": "invalid"}, status_code=413)
        try:
            body = PairBody.model_validate(json.loads(raw))
        except (ValidationError, ValueError, RecursionError):
            return JSONResponse({"error": "invalid"}, status_code=400)
        redeemed = await devices.redeem(body.code, name=body.name, fallback_name=body.default_name)
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

    def take_slot(peer: str) -> bool:
        if (
            sum(preauth.values()) >= MAX_PREAUTH_TOTAL
            or preauth.get(peer, 0) >= MAX_PREAUTH_PER_ADDRESS
        ):
            return False
        preauth[peer] = preauth.get(peer, 0) + 1
        return True

    def release_slot(peer: str) -> None:
        left = preauth.get(peer, 0) - 1
        if left > 0:
            preauth[peer] = left
        else:
            preauth.pop(peer, None)

    async def bridge(websocket: WebSocket) -> None:
        peer = _client_address(websocket.client)
        if not _same_origin(websocket.headers):
            log.warning("lan.ws_origin_refused", peer=peer)
            await websocket.close(code=CLOSE_POLICY_VIOLATION)
            return
        if not take_slot(peer):
            log.warning("lan.ws_preauth_full", peer=peer)
            await websocket.close(code=CLOSE_TRY_AGAIN_LATER)
            return
        try:
            await websocket.accept()
            # The hub is dialled only for a client that speaks first: an open socket that never
            # sends anything costs the hub nothing.
            try:
                first = await asyncio.wait_for(websocket.receive(), FIRST_FRAME_TIMEOUT_S)
            except TimeoutError:
                await websocket.close(code=CLOSE_POLICY_VIOLATION)
                return
            if first["type"] == "websocket.disconnect" or first.get("text") is None:
                with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                    await websocket.close(code=CLOSE_POLICY_VIOLATION)
                return
            headers = {LAN_BRIDGE_HEADER: bridge_secret(), LAN_PEER_HEADER: peer}
            try:
                upstream = await connect(
                    hub_url(),
                    additional_headers=headers,
                    max_size=max_frame_bytes,
                    compression=None,
                    open_timeout=5,
                    ping_interval=None,
                )
            except (OSError, InvalidHandshake, TimeoutError) as exc:
                log.warning("lan.hub_unreachable", error=f"{type(exc).__name__}: {exc}")
                await websocket.close(code=CLOSE_INTERNAL_ERROR)
                return
            await upstream.send(first["text"])
            try:
                answer = await asyncio.wait_for(upstream.recv(), FIRST_FRAME_TIMEOUT_S)
            except (TimeoutError, ConnectionClosed):
                await upstream.close()
                with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                    await websocket.close(code=CLOSE_POLICY_VIOLATION)
                return
            await websocket.send_text(answer if isinstance(answer, str) else answer.decode())
        finally:
            release_slot(peer)
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
            close_code = _close_code(upstream.close_code)

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

    async def not_found(_request: Request) -> Response:
        return Response("not found\n", status_code=404)

    app = Starlette(routes=routes)
    app.add_middleware(
        _Guard, allowed_hosts=allowed_hosts, allow_public_clients=allow_public_clients
    )
    return app
