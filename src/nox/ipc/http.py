"""The core's loopback HTTP server: `/health`, `/pet`, `/dashboard` and `/api/*`.

A Starlette app run by uvicorn inside the core's own event loop.

`/health` needs no authentication and therefore carries nothing an unauthenticated local process
should not see: component states and reasons, no paths and no identifiers. `/api/*` requires
`Authorization: Bearer <session token>` and is where anything more detailed lives. A missing UI
bundle is served as an explicit "UI not built" page with status 503, never as a fake UI.

Hardening: `/pet`, `/dashboard` and `/api/*` responses carry `Cache-Control: no-store`,
`Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`, and uvicorn's access log is
off, so request URLs never reach a log file.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import inspect
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Generator, Mapping
from pathlib import Path
from typing import Any

import uvicorn
from pydantic import BaseModel, Field, field_validator
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nox.core.config import IpcConfig
from nox.ipc._log import get_logger
from nox.ipc.server import write_runtime_info
from nox.ipc.tokens import constant_time_equals

log = get_logger(__name__)

JsonLike = Mapping[str, Any] | list[Any]
Provider = Callable[[], JsonLike | Awaitable[JsonLike]]
StateProvider = Callable[[str | None], JsonLike | Awaitable[JsonLike]]
TokenGetter = Callable[[], str]

_NO_STORE = {"Cache-Control": "no-store"}
SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_HARDENED_PREFIXES = ("/pet", "/dashboard", "/api")


class _SecurityHeaders:
    """Pure-ASGI middleware adding SECURITY_HEADERS to /pet, /dashboard and /api responses."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not any(path == p or path.startswith(p + "/") for p in _HARDENED_PREFIXES):
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    headers[key] = value
            await send(message)

        await self._app(scope, receive, send_with_headers)


class HttpSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=47801, ge=0, le=65535)
    port_attempts: int = Field(default=6, ge=1)
    pet_dist: Path | None = None
    dashboard_dist: Path | None = None

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value == "localhost":
            return value
        try:
            if ipaddress.ip_address(value).is_loopback:
                return value
        except ValueError:
            pass
        raise ValueError(f"ipc.host must be a loopback address, got {value!r}")

    @classmethod
    def from_config(cls, ipc: IpcConfig, **overrides: Any) -> HttpSettings:
        """Build from the typed `ipc` section of `NoxConfig`."""
        return cls(host=ipc.host, port=ipc.http_port, **overrides)


async def _resolve(value: JsonLike | Awaitable[JsonLike]) -> JsonLike:
    if inspect.isawaitable(value):
        return await value
    return value


def _placeholder(name: str, expected: Path | None) -> Callable[[Request], Awaitable[Response]]:
    where = html.escape(str(expected)) if expected else "(no directory configured)"
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>Nox {name}: UI not built</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:3rem;max-width:40rem;color:#222}"
        "code{background:#eee;padding:.1rem .3rem}</style></head><body>"
        f"<h1>Nox {name} UI is not built</h1>"
        f"<p>The core is running, but no UI bundle was found at <code>{where}</code>.</p>"
        f"<p>Build the {name} front-end (Vite, see ADR-005) or point <code>ipc.{name}_dist</code> "
        "at a built <code>dist</code> directory, then reload this page.</p>"
        "</body></html>"
    )

    async def endpoint(_request: Request) -> Response:
        return HTMLResponse(body, status_code=503, headers=_NO_STORE)

    return endpoint


def _ui_route(path: str, name: str, dist: Path | None) -> Mount | Route:
    if dist is not None and (dist / "index.html").is_file():
        return Mount(path, app=StaticFiles(directory=str(dist), html=True), name=name)
    log.warning("ui_bundle_missing", ui=name, expected=str(dist) if dist else None)
    return Route(path, _placeholder(name, dist), methods=["GET"])


def create_app(
    *,
    health: Provider,
    state: StateProvider,
    providers: Provider,
    session_token: TokenGetter,
    pet_dist: Path | None = None,
    dashboard_dist: Path | None = None,
) -> Starlette:
    """Build the Starlette app. Callables are injected by the core; they must not return secrets."""

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        scheme, _, credential = header.partition(" ")
        if scheme.lower() != "bearer" or not credential.strip():
            return False
        return constant_time_equals(credential.strip(), session_token())

    def unauthorized() -> Response:
        return JSONResponse(
            {"error": "auth.denied", "message": "session token required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer", **_NO_STORE},
        )

    async def health_endpoint(_request: Request) -> Response:
        return JSONResponse(await _resolve(health()), headers=_NO_STORE)

    async def state_endpoint(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        path = request.query_params.get("path") or None
        return JSONResponse(await _resolve(state(path)), headers=_NO_STORE)

    async def providers_endpoint(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        return JSONResponse(await _resolve(providers()), headers=_NO_STORE)

    async def api_fallback(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        return JSONResponse(
            {"error": "not_found", "path": request.url.path}, status_code=404, headers=_NO_STORE
        )

    routes: list[Route | Mount] = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/api/state", state_endpoint, methods=["GET"]),
        Route("/api/providers", providers_endpoint, methods=["GET"]),
        Route("/api/{rest:path}", api_fallback),
        _ui_route("/pet", "pet", pet_dist),
        _ui_route("/dashboard", "dashboard", dashboard_dist),
    ]
    return Starlette(routes=routes, middleware=[Middleware(_SecurityHeaders)])


class _EmbeddedServer(uvicorn.Server):
    """uvicorn.Server that never touches process signal handlers (the core owns SIGINT/SIGTERM)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


class HttpServer:
    """Runs the app with uvicorn on the current loop; tries `port_attempts` consecutive ports."""

    def __init__(
        self, app: Starlette, settings: HttpSettings, *, runtime_dir: Path | None = None
    ) -> None:
        self._app = app
        self._settings = settings
        self._runtime_dir = runtime_dir
        self._server: _EmbeddedServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("http server not started")
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self._settings.host}:{self.port}"

    def _bind(self) -> socket.socket:
        s = self._settings
        candidates = [0] if s.port == 0 else list(range(s.port, s.port + s.port_attempts))
        last_error: OSError | None = None
        for port in candidates:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind((s.host, port))
            except OSError as exc:
                sock.close()
                last_error = exc
                log.warning("http_port_busy", port=port, error=str(exc))
                continue
            return sock
        raise OSError(f"no free HTTP port in {candidates[0]}..{candidates[-1]}") from last_error

    async def start(self, *, startup_timeout: float = 10.0) -> None:
        if self._server is not None:
            raise RuntimeError("http server already started")
        sock = self._bind()
        self._port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            self._app,
            host=self._settings.host,
            port=self._port,
            loop="asyncio",
            lifespan="off",
            log_config=None,
            log_level="warning",
            access_log=False,
            server_header=False,
            date_header=False,
        )
        self._server = _EmbeddedServer(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]), name="nox-http")
        deadline = asyncio.get_running_loop().time() + startup_timeout
        while not self._server.started:
            if self._task.done():
                exc = self._task.exception()
                self._server = None
                raise RuntimeError("http server failed to start") from exc
            if asyncio.get_running_loop().time() > deadline:
                await self.stop()
                raise TimeoutError("http server did not start in time")
            await asyncio.sleep(0.01)
        if self._runtime_dir is not None:
            write_runtime_info(self._runtime_dir, http_port=self._port, http_url=self.url)
        log.info("http_server_started", url=self.url)

    async def stop(self) -> None:
        server, task = self._server, self._task
        self._server, self._task = None, None
        if server is None or task is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        self._port = None
        log.info("http_server_stopped")
