"""The core's half of the voice contract: health from heartbeats, mute and capture handed to a
(re)registering worker, refused push-to-talk announced, and the state a UI resyncs from."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nox.core.boot.health import core_health_checks
from nox.core.boot.voice_health import STALE_AFTER_S, VoiceHealthReport
from nox.core.boot.workers import WorkerSupervisor
from nox.core.events import E, HealthStatus
from nox.core.jobobject import JobObject
from nox.ipc.dispatch import RequestContext
from nox.ipc.handlers.core import (
    CoreHandlers,
    PrivacyStatus,
    VoicePtt,
    WorkerHeartbeat,
    WorkerRegister,
)
from nox.ipc.protocol import Envelope, Kind, Source
from nox.security.privacy import PrivacyService
from tests.unit.fakes import FakeBus, FakeState


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class KillSwitch:
    def __init__(self) -> None:
        self.engaged = False

    def is_engaged(self) -> bool:
        return self.engaged


class Hub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.services: dict[str, set[str]] = {}

    async def request(
        self, client_id: str, name: str, payload: dict[str, Any], *, timeout: float = 0
    ) -> dict[str, Any]:
        self.requests.append((client_id, name, payload))
        return {"ok": True}

    def declare_services(self, client_id: str, services: set[str]) -> None:
        self.services[client_id] = services


class Health:
    def __init__(self) -> None:
        self.runs = 0

    async def run_once(self) -> None:
        self.runs += 1


def make_core(tmp_path: Any, clock: Clock | None = None) -> SimpleNamespace:
    workers = WorkerSupervisor(
        job=JobObject(), command=["x"], cwd=tmp_path, hub_url=lambda: "", data_dir=tmp_path
    )
    privacy = PrivacyService(zones=["banking"])
    killswitch = KillSwitch()
    privacy.set_safe_mode_source(killswitch.is_engaged)
    tasks: list[Any] = []

    def spawn_task(coro: Any) -> None:
        tasks.append(coro)

    return SimpleNamespace(
        workers=workers,
        voice_report=VoiceHealthReport(clock=clock or Clock()),
        security=SimpleNamespace(
            privacy=privacy,
            killswitch=killswitch,
            connect_state=lambda: {
                "privacy_mode": privacy.mode.value,
                "capture": privacy.effective_capture().model_dump(mode="json"),
                "safe_mode": killswitch.is_engaged(),
            },
        ),
        state=FakeState(),
        bus=FakeBus(),
        hub=Hub(),
        health=Health(),
        config=SimpleNamespace(voice=SimpleNamespace(model_dump=lambda mode: {})),
        spawn_task=spawn_task,
        tasks=tasks,
    )


def ctx(client_id: str, role: str) -> RequestContext:
    env = Envelope(kind=Kind.REQUEST, name="test.request", src=Source(role=role, id=client_id))
    return RequestContext(client_id=client_id, role=role, request=env)


def ready_voice_worker(core: SimpleNamespace) -> None:
    worker = core.workers.attach("voice", "worker:voice")
    worker.registered.set()


# ---- health from the heartbeat --------------------------------------------------------------


def test_report_summary_names_every_component_that_is_not_available() -> None:
    report = VoiceHealthReport(clock=Clock())
    assert report.summary() is None
    report.update(
        {
            "capture": (HealthStatus.UNAVAILABLE, "no audio from the microphone for 5 s"),
            "echo": (HealthStatus.LIMITED, "half-duplex (no echo cancellation)"),
            "stt": (HealthStatus.AVAILABLE, "small/int8 on cpu"),
        }
    )
    status, reason = report.summary() or (None, "")
    assert status is HealthStatus.UNAVAILABLE
    assert "capture: no audio from the microphone" in reason
    assert "echo: half-duplex (no echo cancellation)" in reason
    assert "stt" not in reason


def test_a_report_that_stopped_arriving_is_stale() -> None:
    clock = Clock()
    report = VoiceHealthReport(clock=clock)
    report.update({"capture": (HealthStatus.AVAILABLE, "capturing")})
    assert report.summary() == (HealthStatus.AVAILABLE, "worker registered")
    clock.now += STALE_AFTER_S + 1
    status, reason = report.summary() or (None, "")
    assert status is HealthStatus.LIMITED and "no heartbeat" in reason


async def test_a_dead_microphone_reaches_the_voice_health_check(tmp_path: Any) -> None:
    core = make_core(tmp_path)
    ready_voice_worker(core)
    handlers = CoreHandlers(core)  # type: ignore[arg-type]
    beat = WorkerHeartbeat.model_validate(
        {
            "status": "running",
            "health": {
                "capture": {
                    "status": "unavailable",
                    "reason": "no audio from the microphone for 4 s (device unplugged)",
                },
                "stt": {"status": "available", "reason": "small"},
            },
        }
    )
    await handlers.worker_heartbeat(ctx("worker:voice", "worker"), beat)
    checks = core_health_checks(
        database=lambda: None,
        vault_dir=lambda: tmp_path,
        workers=core.workers,
        voice_enabled=True,
        tokens=lambda: None,
        providers=lambda: [],
        voice_report=lambda: core.voice_report,
    )
    voice = next(c for c in checks if c.name == "voice")
    status, reason = await voice.probe()
    assert status is HealthStatus.UNAVAILABLE
    assert "device unplugged" in reason
    assert core.tasks, "a changed report re-runs health at once"
    for task in core.tasks:
        await task


async def test_a_heartbeat_from_another_client_cannot_write_the_voice_report(
    tmp_path: Any,
) -> None:
    core = make_core(tmp_path)
    ready_voice_worker(core)
    beat = WorkerHeartbeat.model_validate(
        {"health": {"capture": {"status": "unavailable", "reason": "spoofed"}}}
    )
    await CoreHandlers(core).worker_heartbeat(ctx("plugin:x", "plugin"), beat)  # type: ignore[arg-type]
    assert core.voice_report.summary() is None


# ---- worker (re)registration --------------------------------------------------------------


async def test_register_hands_the_worker_the_cores_mute_flag(tmp_path: Any) -> None:
    core = make_core(tmp_path)
    await core.state.update("assistant.muted", True)
    response = await CoreHandlers(core).worker_register(  # type: ignore[arg-type]
        ctx("worker:voice", "worker"), WorkerRegister(service="voice", pid=1)
    )
    assert response["muted"] is True
    assert response["capture"]["microphone"] is True
    for task in core.tasks:
        await task


# ---- push-to-talk refusals ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("zone", "privacy_zone"),
        ("mic_off", "microphone_off"),
        ("muted", "muted"),
        ("killed", "safe_mode"),
        ("no_worker", "voice_unavailable"),
    ],
)
async def test_a_refused_push_to_talk_is_announced_not_swallowed(
    tmp_path: Any, setup: str, reason: str
) -> None:
    core = make_core(tmp_path)
    if setup != "no_worker":
        ready_voice_worker(core)
    privacy: PrivacyService = core.security.privacy
    if setup == "zone":
        await privacy.observe_foreground("Sparkasse KölnBonn - Online-Banking", "chrome.exe")
    elif setup == "mic_off":
        await privacy.set_capture(microphone=False, by="test")
    elif setup == "muted":
        await core.state.update("assistant.muted", True)
    elif setup == "killed":
        core.security.killswitch.engaged = True
    result = await CoreHandlers(core).voice_ptt(  # type: ignore[arg-type]
        ctx("shell:main", "shell"), VoicePtt(pressed=True)
    )
    assert result["ok"] is False and result["reason"] == reason
    refused = [e.payload for e in core.bus.published if e.name == E.VOICE_PTT_REFUSED]
    assert [p["reason"] for p in refused] == [reason]
    if setup == "zone":
        assert refused[0]["zone"] == "banking"
    assert core.hub.requests == []  # the worker was never asked to open the microphone


async def test_an_allowed_push_to_talk_reaches_the_worker(tmp_path: Any) -> None:
    core = make_core(tmp_path)
    ready_voice_worker(core)
    result = await CoreHandlers(core).voice_ptt(  # type: ignore[arg-type]
        ctx("shell:main", "shell"), VoicePtt(pressed=True)
    )
    assert result == {"ok": True}
    assert core.hub.requests == [("worker:voice", "voice.ptt", {"pressed": True})]


async def test_a_release_is_always_forwarded_even_inside_a_zone(tmp_path: Any) -> None:
    core = make_core(tmp_path)
    ready_voice_worker(core)
    await core.security.privacy.observe_foreground("Online-Banking", "")
    result = await CoreHandlers(core).voice_ptt(  # type: ignore[arg-type]
        ctx("shell:main", "shell"), VoicePtt(pressed=False)
    )
    assert result == {"ok": True}
    assert core.hub.requests == [("worker:voice", "voice.ptt", {"pressed": False})]


# ---- the state a UI starts from -----------------------------------------------------------


async def test_privacy_status_is_the_effective_picture(tmp_path: Any) -> None:
    core = make_core(tmp_path)
    await core.state.update("assistant.muted", True)
    await core.security.privacy.observe_foreground("Online-Banking", "")
    status = await CoreHandlers(core).privacy_status(  # type: ignore[arg-type]
        ctx("shell:main", "shell"), PrivacyStatus()
    )
    assert status["muted"] is True
    assert status["zone_active"] is True
    assert status["capture"]["microphone"] is False
    assert status["safe_mode"] is False
