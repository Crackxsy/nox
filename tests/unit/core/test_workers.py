"""WorkerSupervisor: a crashed voice worker comes back with backoff, and health says why it fell."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from nox.core.boot.workers import RestartPolicy, WorkerSupervisor
from nox.core.events import HealthStatus
from nox.core.jobobject import JobObject
from nox.ipc.tokens import TokenStore
from tests.unit.fakes import MonotonicClock


class FakeProcess:
    """Alive until `exit(code)`; `terminate` ends it like a well-behaved worker."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def exit(self, code: int) -> None:
        self.returncode = code

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode if self.returncode is not None else 0


class Harness:
    def __init__(self, *, policy: RestartPolicy | None = None) -> None:
        self.clock = MonotonicClock()
        self.processes: list[FakeProcess] = []
        self.tokens = TokenStore(alive=lambda _pid, _created: True)
        self.gate_open = True
        self.changes = 0
        self.workers = WorkerSupervisor(
            job=JobObject("nox-test-workers"),
            command=["python", "-m", "nox.worker"],
            cwd=Path("."),
            hub_url=lambda: "ws://127.0.0.1:1/ws",
            data_dir=Path("."),
            policy=policy or RestartPolicy(),
            clock=self.clock,
            process_factory=self._spawn,
        )
        self.workers.use_tokens(self.tokens)
        self.workers.use_respawn_gate(lambda: self.gate_open, self._changed)

    def _spawn(self, command: Sequence[str], env: Mapping[str, str], cwd: Path) -> Any:
        assert "--service" in command and env["NOX_WORKER_TOKEN"]
        process = FakeProcess(pid=5000 + len(self.processes))
        self.processes.append(process)
        return process

    def _changed(self) -> None:
        self.changes += 1

    @property
    def current(self) -> FakeProcess:
        return self.processes[-1]

    def crash(self, code: int = 1, *, reason: str = "") -> None:
        if reason:
            self.workers.report_failure("voice", reason)
        self.current.exit(code)
        self.workers.check_processes()


def test_a_crashed_worker_is_respawned_after_a_growing_delay() -> None:
    h = Harness()
    h.workers.spawn("voice")

    delays = []
    for _ in range(3):
        h.crash(1)
        before = len(h.processes)
        status, reason = h.workers.health("voice")
        assert status is HealthStatus.UNAVAILABLE and "restarting in" in reason
        waited = 0.0
        while len(h.processes) == before:
            h.clock.advance(0.5)
            waited += 0.5
            h.workers.check_processes()
        delays.append(waited)

    assert delays == [1.0, 2.0, 4.0]
    assert "voice" in h.workers
    assert h.changes >= 6  # every exit and every respawn refreshed health


def test_the_backoff_is_capped() -> None:
    policy = RestartPolicy(initial_backoff_s=1.0, max_backoff_s=60.0)
    assert [policy.delay_for(n) for n in (1, 6, 7, 20)] == [1.0, 32.0, 60.0, 60.0]


def test_the_worker_gives_up_and_health_names_the_reported_reason() -> None:
    h = Harness(policy=RestartPolicy(max_restarts=2, window_s=600.0))
    h.workers.spawn("voice")
    for _ in range(2):
        h.crash(1, reason="FileNotFoundError: voice model missing: de_DE-thorsten-medium.onnx")
        h.clock.advance(60)
        h.workers.check_processes()

    h.crash(1, reason="FileNotFoundError: voice model missing: de_DE-thorsten-medium.onnx")
    h.clock.advance(3600)
    h.workers.check_processes()

    status, reason = h.workers.health("voice")
    assert status is HealthStatus.UNAVAILABLE
    assert "gave up after 3 failed starts" in reason
    assert "exited with code 1" in reason
    assert "voice model missing: de_DE-thorsten-medium.onnx" in reason
    assert len(h.processes) == 3  # no fourth attempt
    assert "voice" not in h.workers


def test_exits_outside_the_window_do_not_count_toward_giving_up() -> None:
    h = Harness(policy=RestartPolicy(max_restarts=2, window_s=100.0))
    h.workers.spawn("voice")
    for _ in range(5):
        h.crash(1)
        h.clock.advance(150)  # well past the window before the next crash
        h.workers.check_processes()
    assert "voice" in h.workers
    assert len(h.processes) == 6


def test_nothing_is_respawned_while_the_gate_is_closed() -> None:
    h = Harness()
    h.workers.spawn("voice")
    h.gate_open = False
    h.crash(1)
    h.clock.advance(120)
    h.workers.check_processes()
    assert len(h.processes) == 1  # safe mode: no respawn

    h.gate_open = True
    h.workers.check_processes()
    assert len(h.processes) == 2


async def test_a_worker_the_core_terminated_is_not_taken_for_a_crash() -> None:
    h = Harness()
    h.workers.spawn("voice")

    await h.workers.terminate_all()
    h.clock.advance(120)
    h.workers.check_processes()

    assert h.processes[0].terminated
    assert len(h.processes) == 1
    assert h.workers.health("voice") == (HealthStatus.UNAVAILABLE, "worker not running")


def test_an_exit_revokes_the_reconnect_credential_of_that_process() -> None:
    h = Harness()
    h.workers.spawn("voice")
    spawn_token = h.tokens.issue_worker_token("worker:voice")  # stands in for the env token
    secret = h.tokens.authenticate(spawn_token, "worker", "worker:voice").reconnect_token
    assert h.tokens.authenticate(secret, "worker", "worker:voice").ok

    h.crash(1)

    assert not h.tokens.authenticate(secret, "worker", "worker:voice").ok


def test_restart_fresh_forgets_a_worker_that_gave_up() -> None:
    h = Harness(policy=RestartPolicy(max_restarts=0))
    h.workers.spawn("voice")
    h.crash(1)
    assert "gave up" in h.workers.health("voice")[1]

    h.workers.restart_fresh("voice")

    assert "voice" in h.workers
    assert h.workers.health("voice") == (HealthStatus.LIMITED, "worker starting")


def test_health_tells_starting_from_reconnecting() -> None:
    h = Harness()
    h.workers.spawn("voice")
    assert h.workers.health("voice") == (HealthStatus.LIMITED, "worker starting")

    worker = h.workers.attach("voice", "worker:voice")
    worker.registered.set()
    assert h.workers.health("voice") == (HealthStatus.AVAILABLE, "worker registered")

    h.workers.detach("worker:voice")
    status, reason = h.workers.health("voice")
    assert status is HealthStatus.LIMITED and "reconnecting" in reason


@pytest.mark.parametrize("code", [0, 1, 75, -9])
def test_any_exit_of_an_unrequested_worker_is_noticed(code: int) -> None:
    h = Harness()
    h.workers.spawn("voice")
    h.crash(code)
    assert f"exited with code {code}" in h.workers.health("voice")[1]
