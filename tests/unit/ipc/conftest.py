"""Fixtures for nox.ipc tests: in-memory bus, token store, registry, hub factory, raw clients."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from websockets.asyncio.client import ClientConnection, connect

from nox.core.events import Event, Handler
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry, match_name
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.protocol import NAME_AUTH, Envelope, Kind, Source
from nox.ipc.server import HubSettings, IpcHub
from nox.ipc.tokens import TokenStore


class SimpleBus:
    """Minimal EventBus implementation with the dotted-glob semantics of the Event Model."""

    def __init__(self) -> None:
        self.subs: list[tuple[str, Handler]] = []
        self.published: list[Event] = []

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        entry = (pattern, handler)
        self.subs.append(entry)

        def unsubscribe() -> None:
            if entry in self.subs:
                self.subs.remove(entry)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        self.published.append(event)
        for pattern, handler in list(self.subs):
            if match_name(pattern, event.name):
                result = handler(event)
                if result is not None:
                    await result

    async def wait_for(
        self, name: str, *, timeout: float | None = None, corr: str | None = None
    ) -> Event:
        fut: asyncio.Future[Event] = asyncio.get_running_loop().create_future()

        def _h(ev: Event) -> None:
            if not fut.done() and (corr is None or ev.corr == corr):
                fut.set_result(ev)

        unsub = self.subscribe(name, _h)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            unsub()

    def names(self) -> list[str]:
        return [e.name for e in self.published]


class StateGet(BaseModel):
    path: str | None = None


class ModeSet(BaseModel):
    mode: str


class ChatSend(BaseModel):
    text: str
    session_id: str | None = None


class PetInteract(BaseModel):
    type: str
    x: float | None = None
    y: float | None = None


class Flag(BaseModel):
    pressed: bool = False
    muted: bool = False


def build_registry(hub_ref: dict[str, IpcHub]) -> RequestRegistry:
    """Handlers representative of the v0.1 request catalogue (IPC Model)."""
    reg = RequestRegistry()

    async def state_get(ctx: RequestContext, p: StateGet) -> dict[str, Any]:
        return {"path": p.path, "caller": ctx.client_id, "role": ctx.role, "version": 1}

    async def health_get(ctx: RequestContext, p: EmptyPayload) -> dict[str, Any]:
        return {"components": {}}

    async def mode_set(ctx: RequestContext, p: ModeSet) -> dict[str, Any]:
        if p.mode == "crash":
            raise RuntimeError("boom")
        if p.mode == "forbidden":
            raise IpcError(ERR_PERMISSION, "policy says no")
        return {"ok": True, "mode": p.mode}

    async def chat_send(ctx: RequestContext, p: ChatSend) -> dict[str, Any]:
        # OP-9: stream frames are `ChatStreamFrame {delta, done}`, the response `ChatSendResult`.
        for word in p.text.split():
            await ctx.stream({"delta": word})
        await ctx.stream({"delta": ""}, True)
        return {
            "request_id": ctx.request.id,
            "text": p.text.upper(),
            "provider": "test",
            "degraded": False,
        }

    async def ok(ctx: RequestContext, p: BaseModel) -> dict[str, Any]:
        return {"ok": True}

    async def worker_register(ctx: RequestContext, p: BaseModel) -> dict[str, Any]:
        services = list(getattr(p, "services", []))
        hub_ref["hub"].declare_services(ctx.client_id, services)
        return {"ok": True, "config": {}}

    class WorkerRegister(BaseModel):
        service: str
        services: list[str] = []
        capabilities: list[str] = []
        pid: int = 0

    reg.register("state.get", StateGet, state_get)
    reg.register("health.get", EmptyPayload, health_get)
    reg.register("mode.set", ModeSet, mode_set)
    reg.register("chat.send", ChatSend, chat_send)
    reg.register("ai.providers", EmptyPayload, ok)
    reg.register("privacy.set", EmptyPayload, ok)
    reg.register("security.kill", EmptyPayload, ok)
    reg.register("security.panic", EmptyPayload, ok)
    reg.register("security.permission.reply", EmptyPayload, ok)
    reg.register("voice.ptt", Flag, ok)
    reg.register("voice.mute", Flag, ok)
    reg.register("pet.interact", PetInteract, ok)
    reg.register("worker.register", WorkerRegister, worker_register)
    reg.register("worker.heartbeat", EmptyPayload, ok)
    return reg


@pytest.fixture
def bus() -> SimpleBus:
    return SimpleBus()


@pytest.fixture
def tokens() -> TokenStore:
    return TokenStore()


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runtime"
    d.mkdir()
    return d


@pytest.fixture
def hub_ref() -> dict[str, IpcHub]:
    return {}


@pytest.fixture
def registry(hub_ref: dict[str, IpcHub]) -> RequestRegistry:
    return build_registry(hub_ref)


HubFactory = Callable[..., Awaitable[IpcHub]]


@pytest.fixture
async def hub_factory(
    bus: SimpleBus,
    tokens: TokenStore,
    registry: RequestRegistry,
    runtime_dir: Path,
    hub_ref: dict[str, IpcHub],
) -> AsyncIterator[HubFactory]:
    hubs: list[IpcHub] = []

    async def make(**settings: Any) -> IpcHub:
        settings.setdefault("port", 0)
        hub = IpcHub(HubSettings(**settings), tokens, registry, bus, runtime_dir)
        await hub.start()
        hubs.append(hub)
        hub_ref["hub"] = hub
        return hub

    yield make
    for hub in hubs:
        await hub.stop()


@pytest.fixture
async def hub(hub_factory: HubFactory) -> IpcHub:
    return await hub_factory()


class RawClient:
    """Speaks raw Envelope JSON so tests control every frame."""

    def __init__(self, conn: ClientConnection, source: Source) -> None:
        self.conn = conn
        self.source = source

    async def send(
        self, kind: Kind, name: str, payload: dict[str, Any] | None = None, **kw: Any
    ) -> Envelope:
        env = Envelope(kind=kind, name=name, src=self.source, payload=payload or {}, **kw)
        await self.conn.send(env.model_dump_json())
        return env

    async def send_raw(self, text: str | bytes) -> None:
        await self.conn.send(text)

    async def recv(self, timeout: float = 3.0) -> Envelope:
        raw = await asyncio.wait_for(self.conn.recv(), timeout)
        return Envelope.model_validate_json(raw)

    async def recv_named(self, name: str, timeout: float = 3.0) -> Envelope:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            env = await self.recv(max(0.01, deadline - asyncio.get_running_loop().time()))
            if env.name == name:
                return env

    async def auth(self, token: str, **extra: Any) -> Envelope:
        payload = {"token": token, "role": self.source.role, "id": self.source.id, **extra}
        await self.send(Kind.REQUEST, NAME_AUTH, payload)
        return await self.recv()

    async def request(self, name: str, payload: dict[str, Any] | None = None) -> Envelope:
        env = await self.send(Kind.REQUEST, name, payload)
        while True:
            reply = await self.recv()
            if reply.corr == env.id and reply.kind in (Kind.RESPONSE, Kind.ERROR):
                return reply

    async def close(self) -> None:
        await self.conn.close()

    async def wait_closed(self, timeout: float = 3.0) -> int | None:
        await asyncio.wait_for(self.conn.wait_closed(), timeout)
        return self.conn.close_code


@pytest.fixture
async def raw(hub: IpcHub) -> AsyncIterator[Callable[..., Awaitable[RawClient]]]:
    conns: list[RawClient] = []

    async def open_client(
        role: str = "shell", id: str = "shell:1", url: str | None = None
    ) -> RawClient:
        conn = await connect(url or hub.url, open_timeout=5)
        client = RawClient(conn, Source(role=role, id=id))  # type: ignore[arg-type]
        conns.append(client)
        return client

    yield open_client
    for c in conns:
        await c.close()


async def authed(
    raw: Callable[..., Awaitable[RawClient]], tokens: TokenStore, role: str, id: str
) -> RawClient:
    c = await raw(role, id)
    reply = await c.auth(tokens.session_token)
    assert reply.kind is Kind.RESPONSE, reply.payload
    return c


def dumps(obj: Any) -> str:
    return json.dumps(obj)
