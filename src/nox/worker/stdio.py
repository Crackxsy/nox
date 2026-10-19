"""A plugin worker that talks to the core over stdin/stdout - a self-built skill in the sandbox.

A sandboxed process reaches nothing on the machine: not the hub's loopback port, not `<nox home>`,
not the credential store. So a self-built skill's worker gets no hub token and no data folder. It
runs inside `nox.sandbox` (`nox.skills.worker_launch`), receives its files as a bundle on stdin
(`nox.skills.bundle`), and then speaks newline-delimited JSON on the same pipes; the core relays
what it is allowed to say to the hub with the token the core keeps for it. The worker itself is
the ordinary `PluginWorker` with this module's `StdioClient` in place of the WebSocket client, so a
skill sees exactly the same `PluginApi` as any other plugin - secrets per call through the core
(`plugin.secret.get`, declared names only), HTTP only through the sandbox's egress proxy.

Messages (one JSON object per line):

* worker -> core: `{"t": "req", "id", "name", "payload", "timeout"}`, `{"t": "evt", "name",
  "payload"}`, `{"t": "sub", "patterns"}`, and `{"t": "res", "id", "ok", "payload" | "error"}`
  for a request of the core;
* core -> worker: `{"t": "res", ...}` for a worker request, `{"t": "evt", "env": <envelope>}`,
  and `{"t": "req", "id", "name", "payload"}` (`tool.call`, `plugin.stop`).

Anything the plugin prints goes to stderr: before the plugin is imported, file descriptor 1 and
`sys.stdout` are pointed at stderr and the protocol keeps a private duplicate of the pipe, so a
stray `print` cannot break the protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from nox.ipc.dispatch import match_name
from nox.ipc.errors import ERR_INTERNAL, ERR_NOT_FOUND, ERR_TIMEOUT, ERR_UNAVAILABLE, IpcError
from nox.ipc.protocol import Envelope

__all__ = ["WORKER_FILE", "WORKER_SOURCE", "StdioClient", "run_stdio_plugin_worker"]

WORKER_FILE = "_nox_skill_worker.py"
#: The entry the bootstrap runs once the bundle is unpacked in the sandbox's work folder.
WORKER_SOURCE = "import sys\nfrom nox.worker.stdio import main\nsys.exit(main(sys.argv[2]))\n"

Handler = Callable[[dict[str, Any], Any], Awaitable[Any]]
EventHandler = Callable[[Envelope], Awaitable[None] | None]


class StdioClient:
    """The `IpcClient` surface `PluginWorker` uses, over a pair of pipes."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO, *, request_timeout_s: float = 30.0):
        self._reader = reader
        self._writer = writer
        self._timeout = request_timeout_s
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: dict[str, Handler] = {}
        self._event_handlers: list[tuple[str, EventHandler]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pump: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()

    # -- the IpcClient surface ----------------------------------------------------------------

    async def connect(self) -> None:
        if self._pump is None:
            self._pump = asyncio.create_task(self._read_loop(), name="nox-stdio-reader")

    async def close(self) -> None:
        self.closed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(IpcError(ERR_UNAVAILABLE, "closed"))
        with contextlib.suppress(OSError, ValueError):
            self._writer.flush()

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        rid = str(next(self._ids))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        wait = timeout or self._timeout
        self._write({"t": "req", "id": rid, "name": name, "payload": dict(payload or {}),
                     "timeout": wait})  # fmt: skip
        try:
            return await asyncio.wait_for(future, wait)
        except TimeoutError as exc:
            raise IpcError(ERR_TIMEOUT, f"{name} timed out") from exc
        finally:
            self._pending.pop(rid, None)

    async def send_event(
        self, name: str, payload: Mapping[str, Any] | None = None, *, corr: str | None = None
    ) -> None:
        del corr
        self._write({"t": "evt", "name": name, "payload": dict(payload or {})})

    async def subscribe(
        self, patterns: Iterable[str], handler: EventHandler | None = None
    ) -> list[str]:
        listed = list(patterns)
        if handler is not None:
            self._event_handlers.extend((p, handler) for p in listed)
        self._write({"t": "sub", "patterns": listed})
        return listed

    def on(self, name_glob: str, handler: EventHandler) -> Callable[[], None]:
        entry = (name_glob, handler)
        self._event_handlers.append(entry)

        def remove() -> None:
            if entry in self._event_handlers:
                self._event_handlers.remove(entry)

        return remove

    def handle(self, name: str, handler: Handler) -> None:
        self._handlers[name] = handler

    # -- the pipes ------------------------------------------------------------------------------

    def _write(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            self._writer.write(data.encode("utf-8"))
            self._writer.flush()

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                line = await loop.run_in_executor(None, self._reader.readline)
                if not line:
                    return
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        finally:
            await self.close()

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "res":
            future = self._pending.get(str(message.get("id")))
            if future is None or future.done():
                return
            if message.get("ok"):
                payload = message.get("payload")
                future.set_result(payload if isinstance(payload, dict) else {})
            else:
                error = message.get("error")
                body = error if isinstance(error, dict) else {}
                future.set_exception(IpcError.from_payload(body))
        elif kind == "evt":
            try:
                env = Envelope.model_validate(message.get("env"))
            except ValueError:
                return
            for pattern, handler in list(self._event_handlers):
                if match_name(pattern, env.name):
                    self._spawn(_call(handler, env))
        elif kind == "req":
            self._spawn(self._answer(message))

    async def _answer(self, message: dict[str, Any]) -> None:
        rid = message.get("id")
        name = str(message.get("name", ""))
        handler = self._handlers.get(name)
        try:
            if handler is None:
                raise IpcError(ERR_NOT_FOUND, f"the worker does not handle {name!r}")
            payload = message.get("payload")
            result = await handler(payload if isinstance(payload, dict) else {}, None)
            body = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            self._write({"t": "res", "id": rid, "ok": True, "payload": dict(body or {})})
        except IpcError as exc:
            self._write({"t": "res", "id": rid, "ok": False, "error": exc.to_payload()})
        except Exception as exc:  # noqa: BLE001 - the core gets an error, the worker keeps running
            error = IpcError(ERR_INTERNAL, f"{name} failed", details={"type": type(exc).__name__})
            self._write({"t": "res", "id": rid, "ok": False, "error": error.to_payload()})


async def _call(handler: EventHandler, env: Envelope) -> None:
    with contextlib.suppress(Exception):
        result = handler(env)
        if asyncio.iscoroutine(result):
            await result


def _protocol_pipes() -> tuple[BinaryIO, BinaryIO]:
    """stdin as is (the bootstrap already read the bundle line from the same buffer), and a
    private duplicate of stdout; fd 1 and `sys.stdout` become stderr for everything else."""
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return sys.stdin.buffer, out


async def run_stdio_plugin_worker(plugin_id: str, directory: Path) -> int:
    """Load the plugin from `directory` (the sandbox's work folder) and serve it over stdio."""
    import httpx

    from nox.plugins.api import PluginApi, PrivacyView
    from nox.plugins.manifest import MANIFEST_FILE, load_manifest
    from nox.worker.plugin import PluginWorker, load_entry

    reader, writer = _protocol_pipes()
    manifest = load_manifest(directory / MANIFEST_FILE, expected_id=plugin_id)
    factory = load_entry(manifest, directory)
    client = StdioClient(reader, writer)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    privacy = PrivacyView()
    api = PluginApi(
        manifest=manifest,
        client=client,
        privacy=privacy,
        # Inside the sandbox the only way out is the egress proxy of this one process.
        transport_factory=(lambda: httpx.AsyncHTTPTransport(proxy=proxy)) if proxy else None,
    )
    worker = PluginWorker(client=client, manifest=manifest, factory=factory, api=api)
    worker.privacy = privacy
    stopper = asyncio.create_task(_stop_when_closed(client, worker))
    try:
        await worker.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await worker.shutdown()
        return 1
    finally:
        stopper.cancel()
    return 0


async def _stop_when_closed(client: StdioClient, worker: Any) -> None:
    await client.closed.wait()
    worker.request_stop()


def main(plugin_id: str) -> int:
    """Entry of `WORKER_SOURCE`: the plugin's files are in the current (work) folder."""
    return asyncio.run(run_stdio_plugin_worker(plugin_id, Path.cwd()))
