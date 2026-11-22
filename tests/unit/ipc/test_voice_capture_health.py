"""What the core does with the voice worker's microphone report.

The worker says in every heartbeat whether its microphone works; the core turns that into the
`voice` health line the dashboard shows, and refreshes it at once when it changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from nox.core.boot.health import core_health_checks
from nox.core.boot.workers import WorkerSupervisor
from nox.core.events import HealthStatus
from nox.ipc.dispatch import RequestContext
from nox.ipc.handlers.core import CoreHandlers, WorkerHeartbeat
from nox.ipc.protocol import Envelope, Kind, Source


def supervisor() -> WorkerSupervisor:
    return WorkerSupervisor(
        job=cast("Any", None),
        command=["worker"],
        cwd=Path("."),
        hub_url=lambda: "ws://127.0.0.1:1/ws",
        data_dir=Path("."),
    )


async def voice_health(workers: WorkerSupervisor) -> tuple[HealthStatus, str]:
    checks = core_health_checks(
        database=lambda: None,
        vault_dir=lambda: Path("."),
        workers=workers,
        voice_enabled=True,
        tokens=lambda: None,
        providers=lambda: [],
    )
    check = next(c for c in checks if c.name == "voice")
    return await check.probe()


def ready_voice_worker(workers: WorkerSupervisor) -> Any:
    worker = workers.attach("voice", "worker:voice")
    worker.registered.set()
    return worker


class Health:
    def __init__(self) -> None:
        self.runs = 0

    async def run_once(self) -> None:
        self.runs += 1


class Core:
    def __init__(self, workers: WorkerSupervisor) -> None:
        self.workers = workers
        self.health = Health()
        self.spawned = 0

    def spawn_task(self, coro: Any) -> None:
        self.spawned += 1
        coro.close()  # the handler hands over `health.run_once()`; counting it is enough here


def context(role: str = "worker", client: str = "worker:voice") -> RequestContext:
    env = Envelope(
        kind=Kind.REQUEST, name="worker.heartbeat", src=Source(role=role, id=client), payload={}
    )
    return RequestContext(client_id=client, role=role, request=env)


# ---- the voice health line --------------------------------------------------------------------


async def test_a_voice_worker_with_a_working_microphone_is_available() -> None:
    workers = supervisor()
    worker = ready_voice_worker(workers)
    workers.note_capture(worker, "available", "capturing")

    assert await voice_health(workers) == (HealthStatus.AVAILABLE, "worker registered")


@pytest.mark.parametrize("status", ["unavailable", "limited"])
async def test_a_voice_worker_that_cannot_hear_is_limited_and_says_why(status: str) -> None:
    workers = supervisor()
    worker = ready_voice_worker(workers)
    workers.note_capture(worker, status, "no microphone input, retrying in 30 s: no input device")

    got, reason = await voice_health(workers)

    assert got is HealthStatus.LIMITED  # it can still speak, so not unavailable
    assert reason == "microphone: no microphone input, retrying in 30 s: no input device"


async def test_a_worker_that_reports_no_microphone_state_is_not_blamed_for_it() -> None:
    workers = supervisor()
    ready_voice_worker(workers)

    assert await voice_health(workers) == (HealthStatus.AVAILABLE, "worker registered")


async def test_a_worker_that_is_still_loading_stays_limited_whatever_it_reports() -> None:
    workers = supervisor()
    worker = workers.attach("voice", "worker:voice")  # not ready yet
    workers.note_capture(worker, "available", "capturing")

    assert await voice_health(workers) == (HealthStatus.LIMITED, "worker starting")


async def test_the_microphone_state_is_forgotten_when_the_worker_disconnects() -> None:
    workers = supervisor()
    worker = ready_voice_worker(workers)
    workers.note_capture(worker, "unavailable", "gone")

    workers.detach("worker:voice")

    assert worker.capture is None


def test_only_a_changed_report_counts_as_a_change() -> None:
    workers = supervisor()
    worker = ready_voice_worker(workers)

    assert workers.note_capture(worker, "unavailable", "gone") is True
    assert workers.note_capture(worker, "unavailable", "gone") is False
    assert workers.note_capture(worker, "unavailable", "gone for good") is True
    assert workers.note_capture(worker, "", "") is True  # cleared
    assert workers.note_capture(worker, "", "") is False
    assert workers.note_capture(worker, "no-such-status", "x") is False  # unknown reads as empty


# ---- the heartbeat handler --------------------------------------------------------------------


async def test_a_change_of_microphone_state_refreshes_the_health_line_at_once() -> None:
    workers = supervisor()
    ready_voice_worker(workers)
    core = Core(workers)
    handlers = CoreHandlers(cast("Any", core))

    await handlers.worker_heartbeat(
        context(), WorkerHeartbeat(capture="unavailable", capture_reason="gone")
    )
    await handlers.worker_heartbeat(
        context(), WorkerHeartbeat(capture="unavailable", capture_reason="gone")
    )

    assert core.spawned == 1  # the second heartbeat said nothing new
    assert await voice_health(workers) == (HealthStatus.LIMITED, "microphone: gone")

    await handlers.worker_heartbeat(
        context(), WorkerHeartbeat(capture="available", capture_reason="capturing")
    )
    assert core.spawned == 2
    assert await voice_health(workers) == (HealthStatus.AVAILABLE, "worker registered")


async def test_a_heartbeat_from_a_worker_the_core_does_not_know_changes_nothing() -> None:
    workers = supervisor()
    ready_voice_worker(workers)
    core = Core(workers)
    handlers = CoreHandlers(cast("Any", core))

    reply = await handlers.worker_heartbeat(
        context(client="worker:other"), WorkerHeartbeat(capture="unavailable", capture_reason="x")
    )

    assert reply == {"ok": True} and core.spawned == 0


async def test_an_old_heartbeat_without_microphone_fields_is_still_valid() -> None:
    beat = WorkerHeartbeat.model_validate({"load": 0.2, "status": "running"})
    assert (beat.capture, beat.capture_reason) == ("", "")


# ---- pauses between sentences -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("p95_ms", "gaps", "status"),
    [
        (119.9, 50, HealthStatus.AVAILABLE),  # under the budget
        (120.0, 50, HealthStatus.LIMITED),  # at it
        (900.0, 8, HealthStatus.LIMITED),  # enough sentences to judge by
        (900.0, 7, HealthStatus.AVAILABLE),  # one short of that
    ],
)
async def test_pauses_between_sentences_over_the_budget_make_the_voice_limited(
    p95_ms: float, gaps: int, status: HealthStatus
) -> None:
    workers = supervisor()
    worker = ready_voice_worker(workers)
    workers.note_capture(worker, "available", "capturing")
    workers.note_speech_gap(worker, p95_ms, gaps)

    got, reason = await voice_health(workers)

    assert got is status
    if status is HealthStatus.LIMITED:
        assert reason == (f"speech pauses between sentences: p95 {p95_ms:g} ms (budget 120 ms)")


async def test_a_broken_microphone_is_named_before_the_pauses() -> None:
    workers = supervisor()
    worker = ready_voice_worker(workers)
    workers.note_capture(worker, "unavailable", "gone")
    workers.note_speech_gap(worker, 900.0, 50)

    assert await voice_health(workers) == (HealthStatus.LIMITED, "microphone: gone")


def test_a_gap_report_without_numbers_clears_it() -> None:
    workers = supervisor()
    worker = ready_voice_worker(workers)
    workers.note_speech_gap(worker, 80.0, 10)
    assert worker.speech_gap == (80.0, 10)

    workers.note_speech_gap(worker, None, 0)
    assert worker.speech_gap is None
    workers.note_speech_gap(worker, 80.0, 0)  # a number over no gaps is no number
    assert worker.speech_gap is None


async def test_the_pauses_are_forgotten_when_the_worker_disconnects() -> None:
    workers = supervisor()
    worker = ready_voice_worker(workers)
    workers.note_speech_gap(worker, 900.0, 50)

    workers.detach("worker:voice")

    assert worker.speech_gap is None


async def test_the_heartbeat_hands_the_pauses_to_the_health_line_without_refreshing_it() -> None:
    workers = supervisor()
    ready_voice_worker(workers)
    core = Core(workers)
    handlers = CoreHandlers(cast("Any", core))

    await handlers.worker_heartbeat(
        context(),
        WorkerHeartbeat(
            capture="available", capture_reason="capturing", speech_gap_p95_ms=400.0, speech_gaps=20
        ),
    )

    assert core.spawned == 1  # the microphone report is new; the pauses alone would not be
    got, _ = await voice_health(workers)
    assert got is HealthStatus.LIMITED
    await handlers.worker_heartbeat(
        context(),
        WorkerHeartbeat(
            capture="available", capture_reason="capturing", speech_gap_p95_ms=410.0, speech_gaps=21
        ),
    )
    assert core.spawned == 1  # ever-changing numbers must not trigger a probe on every beat


async def test_an_old_heartbeat_without_pause_fields_is_still_valid() -> None:
    beat = WorkerHeartbeat.model_validate({"load": 0.2, "status": "running"})
    assert (beat.speech_gap_p95_ms, beat.speech_gaps) == (None, 0)
