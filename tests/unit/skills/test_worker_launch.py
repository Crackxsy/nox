"""The relay between a sandboxed skill worker and the hub: only the plugin requests get through."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nox.ipc.errors import IpcError
from nox.skills.worker_launch import SandboxedWorker, SkillWorkerLauncher


class Pipe:
    """The sandboxed process as the relay sees it: lines in, lines out."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.pid = 5
        self.running = True
        self.returncode: int | None = None
        self.killed = False

    async def send(self, data: bytes) -> None:
        self.sent.append(json.loads(data))

    async def readline(self) -> bytes:
        return await self.inbox.get()

    def say(self, message: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(message).encode() + b"\n")

    def kill(self) -> None:
        self.killed = True
        self.running = False

    async def stop(self, grace_s: float = 2.0) -> None:
        self.kill()


class Hub:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.handlers: dict[str, Any] = {}

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def request(self, name: str, payload: Any = None, *, timeout: float | None = None) -> Any:
        self.requests.append(name)
        return {"ok": True, "name": name}

    async def send_event(self, name: str, payload: Any = None) -> None: ...

    async def subscribe(self, patterns: Any, handler: Any = None) -> list[str]:
        return list(patterns)

    def handle(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


async def answer_of(pipe: Pipe, rid: str) -> dict[str, Any]:
    for _ in range(200):
        for message in pipe.sent:
            if message.get("t") == "res" and message.get("id") == rid:
                return message
        await asyncio.sleep(0.01)
    raise AssertionError("no answer")


async def test_only_the_plugin_requests_reach_the_hub() -> None:
    pipe, hub = Pipe(), Hub()
    worker = SandboxedWorker(pipe, hub, "dice")
    worker.start()
    pipe.say({"t": "req", "id": "1", "name": "plugin.secret.get", "payload": {"name": "x"}})
    pipe.say({"t": "req", "id": "2", "name": "config.set", "payload": {}})
    pipe.say({"t": "req", "id": "3", "name": "security.permission.reply", "payload": {}})
    assert (await answer_of(pipe, "1"))["ok"] is True
    for rid in ("2", "3"):
        refused = await answer_of(pipe, rid)
        assert refused["ok"] is False and refused["error"]["code"] == "permission.denied"
    assert hub.requests == ["plugin.secret.get"]
    pipe.inbox.put_nowait(b"")
    await asyncio.sleep(0.05)
    assert pipe.killed  # the relay ending takes the sandboxed process with it


async def test_tool_calls_go_to_the_worker_and_come_back() -> None:
    pipe, hub = Pipe(), Hub()
    worker = SandboxedWorker(pipe, hub, "dice")
    worker.start()
    call = asyncio.create_task(hub.handlers["tool.call"]({"name": "dice.roll", "input": {}}))
    await asyncio.sleep(0.05)
    sent = next(m for m in pipe.sent if m.get("t") == "req")
    assert sent["name"] == "tool.call"
    pipe.say({"t": "res", "id": sent["id"], "ok": True, "payload": {"value": 4}})
    assert await asyncio.wait_for(call, 2) == {"value": 4}
    call = asyncio.create_task(hub.handlers["tool.call"]({"name": "dice.roll", "input": {}}))
    await asyncio.sleep(0.05)
    sent = [m for m in pipe.sent if m.get("t") == "req"][-1]
    error = {"code": "internal", "message": "boom"}
    pipe.say({"t": "res", "id": sent["id"], "ok": False, "error": error})
    with pytest.raises(IpcError):
        await asyncio.wait_for(call, 2)
    pipe.inbox.put_nowait(b"")


async def test_without_a_strong_sandbox_nothing_is_started() -> None:
    weak = type("Weak", (), {"guarantees": lambda self: type("G", (), {"strong": False})()})
    launcher = SkillWorkerLauncher(lambda: weak(), module=object())
    assert launcher.unavailable()

    def broken() -> Any:
        raise RuntimeError("no backend")

    assert SkillWorkerLauncher(broken, module=object()).unavailable()
    with pytest.raises(OSError):
        await launcher.spawn(
            plugin_id="dice",
            plugin_dir=None,
            manifest=None,
            hub_url="",
            token="",  # type: ignore[arg-type]
        )
