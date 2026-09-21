"""Spawning and tracking the worker processes, and speaking through the voice one.

A worker is a separate process that connects back to the hub with a one-time token and declares
the services it provides. The core tracks three things per worker: the process, the hub client id
it authenticated as, and whether it has finished loading. The last one matters for health: a
worker that has registered but is still loading its engines is `limited`, not `available`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from nox.core.jobobject import JobObject
from nox.core.logging import get_logger
from nox.ipc.errors import IpcError
from nox.ipc.server import IpcHub
from nox.ipc.tokens import TokenStore
from nox.voice.base import TtsRequest

log = get_logger(__name__)

__all__ = ["WorkerProcess", "WorkerSpeaker", "WorkerSupervisor"]

#: How long a worker gets to exit after being asked to terminate, before it is killed.
TERMINATE_GRACE_S = 2.0


@dataclass
class WorkerProcess:
    """One worker.

    `process` is None when the worker connected without being spawned here; `client_id` is set
    once it has authenticated; `registered` fires when it reports itself ready.
    """

    service: str
    process: subprocess.Popen[bytes] | None
    client_id: str | None = None
    registered: asyncio.Event = field(default_factory=asyncio.Event)


class WorkerSpeaker:
    """The orchestrator's speaker, forwarding text-to-speech to the voice worker over the hub."""

    def __init__(self, hub: IpcHub, client_id: Callable[[], str | None]) -> None:
        self._hub = hub
        self._client_id = client_id

    async def say(self, request: TtsRequest) -> None:
        client_id = self._client_id()
        if client_id is None:
            log.info("speaker.no_voice_worker", utterance_id=request.utterance_id)
            return
        await self._hub.request(
            client_id, "tts.speak", request.model_dump(mode="json"), timeout=120.0
        )

    async def interrupt(self, *, reason: str) -> None:
        client_id = self._client_id()
        if client_id is None:
            return
        with contextlib.suppress(IpcError):
            await self._hub.request(client_id, "tts.stop", {"reason": reason}, timeout=2.0)


class WorkerSupervisor:
    """Owns the worker processes of one core: spawn, look up, register, terminate."""

    def __init__(
        self,
        *,
        job: JobObject,
        command: Sequence[str],
        cwd: Path,
        hub_url: Callable[[], str],
        data_dir: Path,
    ) -> None:
        self._tokens: TokenStore | None = None
        self._job = job
        self._command = list(command)
        self._cwd = cwd
        self._hub_url = hub_url
        self._data_dir = data_dir
        self._workers: dict[str, WorkerProcess] = {}

    @property
    def job(self) -> JobObject:
        """The job object every spawned worker is assigned to, so none outlives the core."""
        return self._job

    def use_tokens(self, tokens: TokenStore) -> None:
        """Hand over the token store once the hub exists; no worker can be spawned before that."""
        self._tokens = tokens

    def __contains__(self, service: str) -> bool:
        return service in self._workers

    def get(self, service: str) -> WorkerProcess | None:
        return self._workers.get(service)

    def all(self) -> Iterable[WorkerProcess]:
        return list(self._workers.values())

    def client_id(self, service: str) -> str | None:
        """The hub client id of a worker that is registered *and* ready, else None."""
        worker = self._workers.get(service)
        return worker.client_id if worker and worker.registered.is_set() else None

    def spawn(self, service: str) -> None:
        if self._tokens is None:
            log.error("worker.spawn_before_ipc", service=service)
            return
        env = dict(os.environ)
        env.update(self._tokens.worker_env(f"worker:{service}"))
        env["NOX_HUB_URL"] = self._hub_url()
        env["NOX_DATA_DIR"] = str(self._data_dir)  # the model files live below it
        command = [*self._command, "--service", service]
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
                command, env=env, cwd=str(self._cwd)
            )
        except OSError as exc:
            log.error("worker.spawn_failed", service=service, error=str(exc))
            return
        self._job.assign(process.pid)
        self._workers[service] = WorkerProcess(service=service, process=process)
        log.info("worker.spawned", service=service, pid=process.pid)

    def attach(self, service: str, client_id: str) -> WorkerProcess:
        """Record the hub connection a worker authenticated on."""
        worker = self._workers.get(service)
        if worker is None:
            worker = WorkerProcess(service=service, process=None)
            self._workers[service] = worker
        worker.client_id = client_id
        return worker

    def detach(self, client_id: str) -> WorkerProcess | None:
        """Forget a hub connection that closed.

        The worker is unavailable until it registers again; without this the core kept routing
        speech and push-to-talk to a dead client id.
        """
        for worker in self._workers.values():
            if worker.client_id == client_id:
                worker.client_id = None
                worker.registered.clear()
                log.warning("worker.disconnected", service=worker.service, client=client_id)
                return worker
        return None

    async def terminate_all(self) -> None:
        for worker in list(self._workers.values()):
            process = worker.process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(process.wait), timeout=TERMINATE_GRACE_S
                    )
                except TimeoutError:
                    log.warning("worker.kill_after_timeout", service=worker.service)
                    process.kill()
            worker.registered.clear()
            worker.client_id = None
        self._workers.clear()
