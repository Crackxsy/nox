"""Spawning, watching and respawning the worker processes, and speaking through the voice one.

A worker is a separate process that connects back to the hub with a one-time token and declares
the services it provides. The core tracks, per worker: the process, the hub client id it
authenticated as, whether it has finished loading, and what it reported before it failed. The
loading state matters for health: a worker that has registered but is still loading its engines is
`limited`, not `available`.

`watch()` polls the processes. One that exits on its own is respawned after a growing delay
(`RestartPolicy`: 1 s, 2 s, 4 s ... at most 60 s); after `max_restarts` exits inside `window_s` the
core stops trying and health reports the worker `unavailable` with its exit code and the reason it
gave - a missing model file, say - instead of "starting" forever. Nothing is respawned while the
respawn gate is closed (safe mode, shutdown), and a worker the core terminates itself is never
mistaken for a crash. Every exit revokes the worker's reconnect credential.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from nox.core.events import HealthStatus
from nox.core.jobobject import JobObject
from nox.core.logging import get_logger
from nox.core.parent_watch import parent_env
from nox.ipc.errors import IpcError
from nox.ipc.server import IpcHub
from nox.ipc.tokens import TokenStore
from nox.voice.base import TtsRequest

log = get_logger(__name__)

__all__ = [
    "TERMINATE_GRACE_S",
    "WORKER_POLL_INTERVAL_S",
    "RestartPolicy",
    "WorkerProcess",
    "WorkerSpeaker",
    "WorkerSupervisor",
]

#: How long a worker gets to exit after being asked to terminate, before it is killed.
TERMINATE_GRACE_S = 2.0

#: How often `watch()` looks at the worker processes.
WORKER_POLL_INTERVAL_S = 1.0

ProcessFactory = Callable[[Sequence[str], Mapping[str, str], Path], "subprocess.Popen[bytes]"]


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """How a crashed worker is brought back, and when the core stops trying."""

    initial_backoff_s: float = 1.0
    max_backoff_s: float = 60.0
    max_restarts: int = 5
    window_s: float = 600.0

    def delay_for(self, attempt: int) -> float:
        """Seconds before restart number `attempt` (1-based): doubling, capped."""
        return float(min(self.initial_backoff_s * 2 ** (attempt - 1), self.max_backoff_s))


@dataclass
class WorkerProcess:
    """One worker.

    `process` is None when the worker connected without being spawned here; `client_id` is set
    once it has authenticated; `registered` fires when it reports itself ready; `connected_once`
    tells a worker that lost its connection from one that never had one; `reported_error` is what
    the worker said about its own failure (`worker.failed`), without paths.
    """

    service: str
    process: subprocess.Popen[bytes] | None
    client_id: str | None = None
    registered: asyncio.Event = field(default_factory=asyncio.Event)
    connected_once: bool = False
    reported_error: str = ""


@dataclass
class _RestartState:
    """What happened to a service's previous processes."""

    exits: list[float] = field(default_factory=list)
    exit_code: int | None = None
    reason: str = ""
    next_attempt_at: float | None = None
    gave_up: bool = False


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


def _popen(command: Sequence[str], env: Mapping[str, str], cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
        list(command), env=dict(env), cwd=str(cwd)
    )


class WorkerSupervisor:
    """Owns the worker processes of one core: spawn, look up, register, watch, terminate."""

    def __init__(
        self,
        *,
        job: JobObject,
        command: Sequence[str],
        cwd: Path,
        hub_url: Callable[[], str],
        data_dir: Path,
        policy: RestartPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        process_factory: ProcessFactory = _popen,
    ) -> None:
        self._tokens: TokenStore | None = None
        self._job = job
        self._command = list(command)
        self._cwd = cwd
        self._hub_url = hub_url
        self._data_dir = data_dir
        self._policy = policy or RestartPolicy()
        self._clock = clock
        self._process_factory = process_factory
        self._workers: dict[str, WorkerProcess] = {}
        self._restarts: dict[str, _RestartState] = {}
        self._may_respawn: Callable[[], bool] = lambda: True
        self._on_change: Callable[[], None] = lambda: None

    @property
    def job(self) -> JobObject:
        """The job object every spawned worker is assigned to, so none outlives the core."""
        return self._job

    @property
    def policy(self) -> RestartPolicy:
        return self._policy

    def use_tokens(self, tokens: TokenStore) -> None:
        """Hand over the token store once the hub exists; no worker can be spawned before that."""
        self._tokens = tokens

    def use_respawn_gate(
        self, may_respawn: Callable[[], bool], on_change: Callable[[], None] = lambda: None
    ) -> None:
        """`may_respawn` closes during safe mode and shutdown; `on_change` refreshes health."""
        self._may_respawn = may_respawn
        self._on_change = on_change

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
        worker_id = f"worker:{service}"
        self._tokens.revoke_worker(worker_id)  # nothing issued to an earlier process stays valid
        env = dict(os.environ)
        env.update(self._tokens.worker_env(worker_id))
        env["NOX_HUB_URL"] = self._hub_url()
        env["NOX_DATA_DIR"] = str(self._data_dir)  # the model files live below it
        env.update(parent_env())  # off Windows, the worker ends itself when the core is gone
        command = [*self._command, "--service", service]
        try:
            process = self._process_factory(command, env, self._cwd)
        except OSError as exc:
            log.error("worker.spawn_failed", service=service, error=str(exc))
            self._record_exit(service, None, f"cannot start the worker: {exc}")
            return
        self._job.assign(process.pid)
        self._tokens.bind_worker_process(worker_id, process.pid)
        self._workers[service] = WorkerProcess(service=service, process=process)
        log.info("worker.spawned", service=service, pid=process.pid)

    def restart_fresh(self, service: str) -> None:
        """Forget earlier failures and spawn now, if not running (resume after safe mode)."""
        self._restarts.pop(service, None)
        if service not in self._workers:
            self.spawn(service)

    def attach(self, service: str, client_id: str) -> WorkerProcess:
        """Record the hub connection a worker authenticated on."""
        worker = self._workers.get(service)
        if worker is None:
            worker = WorkerProcess(service=service, process=None)
            self._workers[service] = worker
        worker.client_id = client_id
        worker.connected_once = True
        return worker

    def report_failure(self, service: str, reason: str) -> None:
        """What a worker said about its own failure, shown in health once it has exited."""
        worker = self._workers.get(service)
        if worker is not None:
            worker.reported_error = reason

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

    # ---- health ------------------------------------------------------------------------------

    def health(self, service: str) -> tuple[HealthStatus, str]:
        """Whether `service` works right now, and if not, why - in words a user can act on."""
        worker = self._workers.get(service)
        if worker is not None and (worker.process is None or worker.process.poll() is None):
            if worker.registered.is_set():
                return HealthStatus.AVAILABLE, "worker registered"
            if worker.connected_once and worker.client_id is None:
                return HealthStatus.LIMITED, "worker lost its connection to the core, reconnecting"
            # Registered but not ready means the engines are still loading: nothing can be
            # spoken yet.
            return HealthStatus.LIMITED, "worker starting"
        state = self._restarts.get(service)
        if state is None:
            return HealthStatus.UNAVAILABLE, "worker not running"
        exited = _describe_exit(state)
        if state.gave_up:
            return (
                HealthStatus.UNAVAILABLE,
                f"worker gave up after {len(state.exits)} failed starts "
                f"in {self._policy.window_s / 60:g} min: {exited}",
            )
        if state.next_attempt_at is not None:
            wait = max(0.0, state.next_attempt_at - self._clock())
            return HealthStatus.UNAVAILABLE, f"{exited}; restarting in {wait:.0f} s"
        return HealthStatus.UNAVAILABLE, exited

    # ---- watching ------------------------------------------------------------------------------

    async def watch(
        self,
        *,
        poll_interval_s: float = WORKER_POLL_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Check the worker processes forever; cancelled by the core's shutdown."""
        while True:
            try:
                self.check_processes()
            except Exception as exc:  # noqa: BLE001 - one bad round must not end the watch
                log.error("worker.watch_failed", error=f"{type(exc).__name__}: {exc}")
            await sleep(poll_interval_s)

    def check_processes(self) -> None:
        """One round: notice exited workers, and respawn the ones whose delay has passed."""
        changed = False
        for service, worker in list(self._workers.items()):
            process = worker.process
            if process is None:
                continue
            code = process.poll()
            if code is None:
                continue
            del self._workers[service]
            self._record_exit(service, code, worker.reported_error)
            changed = True
        now = self._clock()
        for service, state in list(self._restarts.items()):
            due = state.next_attempt_at is not None and now >= state.next_attempt_at
            if not due or service in self._workers or not self._may_respawn():
                continue
            state.next_attempt_at = None
            log.info("worker.respawning", service=service, attempt=len(state.exits))
            self.spawn(service)
            changed = True
        if changed:
            self._on_change()

    def _record_exit(self, service: str, code: int | None, reason: str) -> None:
        if self._tokens is not None:
            self._tokens.revoke_worker(f"worker:{service}")
        now = self._clock()
        state = self._restarts.setdefault(service, _RestartState())
        state.exits = [t for t in state.exits if now - t < self._policy.window_s]
        state.exits.append(now)
        state.exit_code = code
        state.reason = reason
        if len(state.exits) > self._policy.max_restarts:
            state.gave_up = True
            state.next_attempt_at = None
            log.error(
                "worker.gave_up",
                service=service,
                exit_code=code,
                reason=reason,
                exits=len(state.exits),
            )
            return
        delay = self._policy.delay_for(len(state.exits))
        state.next_attempt_at = now + delay
        log.warning(
            "worker.exited", service=service, exit_code=code, reason=reason, restart_in_s=delay
        )

    # ---- shutdown ----------------------------------------------------------------------------

    async def terminate_all(self) -> None:
        """End every worker, concurrently, inside one `TERMINATE_GRACE_S`.

        The workers leave the table before anything is awaited, so the watch never takes a
        requested stop for a crash, and each one's reconnect credential is revoked.
        """
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            worker.registered.clear()
            worker.client_id = None
            if self._tokens is not None:
                self._tokens.revoke_worker(f"worker:{worker.service}")
        await asyncio.gather(*(_terminate(worker) for worker in workers))


async def _terminate(worker: WorkerProcess) -> None:
    process = worker.process
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=TERMINATE_GRACE_S)
    except TimeoutError:
        log.warning("worker.kill_after_timeout", service=worker.service)
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            await asyncio.to_thread(process.wait, TERMINATE_GRACE_S)


def _describe_exit(state: _RestartState) -> str:
    code = (
        "could not be started" if state.exit_code is None else f"exited with code {state.exit_code}"
    )
    return f"worker {code}: {state.reason}" if state.reason else f"worker {code}"
