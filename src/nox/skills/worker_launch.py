"""Starting a self-built skill's worker inside the sandbox, and relaying it to the hub.

An ordinary plugin worker is a process with the user's rights that connects to the hub with a
one-time token. A self-built skill is code a model wrote, so its worker never gets that: it runs in
`nox.sandbox` (`Sandbox.start`, AppContainer / `bwrap`), may reach only the hosts its manifest
declares (through the sandbox's per-process egress proxy), and can read only its own private work
folder - not `<nox home>`, not the keyring, not the hub. Its files arrive as a bundle on stdin
(`nox.skills.bundle`); then it speaks JSON lines on its pipes (`nox.worker.stdio`).

The core side is `SandboxedWorker`: it holds the hub token itself, connects to the hub as
`plugin:<id>` on the worker's behalf, and relays

* the worker's requests - only `plugin.register`, `plugin.secret.get`, `state.get` and
  `worker.heartbeat`; the hub and the `PluginManager` check each one as for any plugin (a secret
  is handed out per call, and only one the manifest declares);
* its events (the hub keeps them inside the skill's namespace) and its subscriptions;
* the core's `tool.call` and `plugin.stop` to the worker.

To the `PluginManager` a `SandboxedWorker` is a process like any other (`pid`, `poll`,
`terminate`, `kill`, `wait`). Without a strong sandbox (`Guarantees.strong`) nothing is started:
`SkillWorkerLauncher.unavailable()` says why and skill activation is refused (fail closed).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.ipc.errors import ERR_INTERNAL, ERR_PERMISSION, ERR_TIMEOUT, IpcError
from nox.ipc.protocol import Envelope
from nox.plugins.manifest import PluginManifest
from nox.skills.bundle import BOOTSTRAP, bundle_files, encode_bundle
from nox.skills.testing import NO_SANDBOX, strong_sandbox
from nox.worker.stdio import WORKER_FILE, WORKER_SOURCE

log = get_logger(__name__)

__all__ = ["ALLOWED_REQUESTS", "SandboxedWorker", "SkillWorkerLauncher"]

#: What a sandboxed skill worker may ask the core for. Everything else is refused here already.
ALLOWED_REQUESTS = frozenset({"plugin.register", "plugin.secret.get", "state.get",
                              "worker.heartbeat"})  # fmt: skip
#: How long the core waits for the worker to answer a tool call or a stop.
WORKER_ANSWER_S = 120.0
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class HubClient(Protocol):
    async def connect(self) -> Any: ...
    async def close(self) -> None: ...
    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]: ...
    async def send_event(self, name: str, payload: Mapping[str, Any] | None = None) -> None: ...
    async def subscribe(self, patterns: Any, handler: Any = None) -> list[str]: ...
    def handle(self, name: str, handler: Any) -> None: ...


ClientFactory = Callable[[str, str, str], HubClient]


def _ipc_client(hub_url: str, token: str, client_id: str) -> HubClient:
    from nox.ipc.client import IpcClient

    return IpcClient(hub_url, token, "plugin", client_id, client_version="0.1.0",
                     request_timeout_s=30.0)  # fmt: skip


class SandboxedWorker:
    """The relay between one sandboxed skill worker and the hub; a `ProcessLike` for the manager."""

    def __init__(self, process: Any, client: HubClient, plugin_id: str) -> None:
        self._process = process
        self._client = client
        self.plugin_id = plugin_id
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._exited = threading.Event()
        self._code: int | None = None
        self._relay: asyncio.Task[None] | None = None

    # -- ProcessLike ----------------------------------------------------------------------------

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    def poll(self) -> int | None:
        if self._process.running:
            return None
        code = self._process.returncode
        return -9 if code is None else int(code)

    def terminate(self) -> None:
        self._spawn(self._process.stop(grace_s=2.0))

    def kill(self) -> None:
        self._process.kill()

    def wait(self, timeout: float | None = None) -> int:
        self._exited.wait(timeout)
        code = self.poll()
        return -9 if code is None else code

    def exited(self, code: int | None) -> None:
        """`on_exit` of the sandboxed process."""
        self._code = code
        self._exited.set()

    # -- relay ----------------------------------------------------------------------------------

    def start(self) -> None:
        self._client.handle("tool.call", self._to_worker("tool.call"))
        self._client.handle("plugin.stop", self._to_worker("plugin.stop"))
        self._relay = asyncio.create_task(self._pump(), name=f"nox-skill-relay-{self.plugin_id}")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        await self._process.send(data.encode("utf-8"))

    def _to_worker(self, name: str) -> Callable[[dict[str, Any], Any], Any]:
        async def forward(payload: dict[str, Any], _request: Any = None) -> dict[str, Any]:
            rid = f"c{next(self._ids)}"
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[rid] = future
            try:
                await self._send({"t": "req", "id": rid, "name": name, "payload": payload})
                return await asyncio.wait_for(future, WORKER_ANSWER_S)
            except TimeoutError as exc:
                raise IpcError(ERR_TIMEOUT, f"the skill did not answer {name}") from exc
            except OSError as exc:
                raise IpcError(ERR_INTERNAL, "the skill's worker has exited") from exc
            finally:
                self._pending.pop(rid, None)

        return forward

    async def _forward_event(self, env: Envelope) -> None:
        with contextlib.suppress(OSError):
            await self._send({"t": "evt", "env": env.model_dump(mode="json")})

    async def _pump(self) -> None:
        try:
            while True:
                line = await self._process.readline()
                if not line:
                    return
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    await self._from_worker(message)
        except Exception as exc:  # noqa: BLE001 - an over-long line or a broken pipe ends it
            log.warning(
                "skills.worker_relay_ended", plugin=self.plugin_id, error=type(exc).__name__
            )
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(OSError("worker exited"))
            self._process.kill()
            with contextlib.suppress(Exception):
                await self._client.close()

    async def _from_worker(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "req":
            self._spawn(self._answer(message))
        elif kind == "evt":
            payload = message.get("payload")
            with contextlib.suppress(IpcError, OSError):
                await self._client.send_event(
                    str(message.get("name", "")), payload if isinstance(payload, dict) else {}
                )
        elif kind == "sub":
            patterns = message.get("patterns")
            if isinstance(patterns, list):
                names = [str(p) for p in patterns][:32]
                with contextlib.suppress(IpcError, OSError):
                    await self._client.subscribe(names, handler=self._forward_event)
        elif kind == "res":
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

    async def _answer(self, message: dict[str, Any]) -> None:
        rid = message.get("id")
        name = str(message.get("name", ""))
        payload = message.get("payload")
        try:
            if name not in ALLOWED_REQUESTS:
                raise IpcError(ERR_PERMISSION, f"a self-built skill may not call {name!r}")
            timeout = message.get("timeout")
            wait = min(float(timeout), 60.0) if isinstance(timeout, (int, float)) else 30.0
            result = await self._client.request(
                name, payload if isinstance(payload, dict) else {}, timeout=wait
            )
            reply: dict[str, Any] = {"t": "res", "id": rid, "ok": True, "payload": result}
        except IpcError as exc:
            reply = {"t": "res", "id": rid, "ok": False, "error": exc.to_payload()}
        with contextlib.suppress(OSError):
            await self._send(reply)


class SkillWorkerLauncher:
    """Builds the sandbox once and starts sandboxed skill workers; fail closed without it."""

    def __init__(
        self,
        sandbox_factory: Callable[[], Any],
        *,
        module: Any = None,
        python: str | None = None,
        client_factory: ClientFactory = _ipc_client,
    ) -> None:
        self._factory = sandbox_factory
        self._module = module
        self._python = python or sys.executable
        self._client_factory = client_factory
        self._sandbox: Any = None
        self._reason: str | None = None
        self._built = False

    def _build(self) -> None:
        if self._built:
            return
        self._built = True
        try:
            if self._module is None:
                import nox.sandbox as module

                self._module = module
            sandbox = self._factory()
        except Exception as exc:  # noqa: BLE001 - no backend: nothing self-built may run
            log.warning("skills.worker_sandbox_unavailable", error=f"{type(exc).__name__}: {exc}")
            self._reason = NO_SANDBOX
            return
        if sandbox is None or not strong_sandbox(sandbox):
            self._reason = NO_SANDBOX
            return
        self._sandbox = sandbox

    def unavailable(self) -> str | None:
        """Why no self-built skill can run here, or None."""
        self._build()
        return self._reason

    async def spawn(
        self,
        *,
        plugin_id: str,
        plugin_dir: Path,
        manifest: PluginManifest,
        hub_url: str,
        token: str,
    ) -> SandboxedWorker:
        reason = self.unavailable()
        if reason is not None:
            raise OSError(reason)
        files = await asyncio.to_thread(bundle_files, plugin_dir, {WORKER_FILE: WORKER_SOURCE})
        data = encode_bundle(files)
        holder: list[SandboxedWorker] = []

        def on_exit(code: int | None) -> None:
            if holder:
                holder[0].exited(code)

        process = await self._sandbox.start(
            [self._python, "-I", "-c", BOOTSTRAP, WORKER_FILE, plugin_id],
            egress=self._module.EgressDecl(hosts=tuple(manifest.network.egress)),
            limits=self._module.Limits(max_message_bytes=MAX_MESSAGE_BYTES),
            on_exit=on_exit,
        )
        client = self._client_factory(hub_url, token, f"plugin:{plugin_id}")
        worker = SandboxedWorker(process, client, plugin_id)
        holder.append(worker)
        try:
            await process.send(data)
            await client.connect()
        except BaseException:
            process.kill()
            raise
        worker.start()
        log.info("skills.worker_started", plugin=plugin_id, pid=worker.pid, sandboxed=True)
        return worker


def sandbox_factory_for(config: Any) -> Callable[[], Any]:
    """`make_sandbox` with the Home Assistant host refused, as for every other sandboxed run."""

    def build() -> Any:
        from nox.sandbox import make_sandbox
        from nox.sandbox.netguard import resolve_all

        host = str(config.home.host or "").strip().lower()
        ips: set[str] = set()
        if host:
            with contextlib.suppress(OSError, ValueError):
                ips = {str(ip) for ip in resolve_all(host, config.home.port)}
        return make_sandbox(
            home_assistant_ips=ips,
            blocked_hosts=[host] if host else [],
            prefer_appcontainer=bool(config.hands.windows_appcontainer),
        )

    return build
