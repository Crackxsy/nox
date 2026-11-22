"""The voice worker steps back from a game: priority follows the mode, threads follow the config."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from nox.core.config import VoiceConfig
from nox.core.events import E
from nox.ipc.protocol import Envelope, Kind, Source
from nox.worker import main as worker_main
from nox.worker.main import VoiceWorker, apply_priority, build_components
from tests.unit.voice.conftest import FakeIpcClient
from tests.unit.voice.test_worker import spy_factory


def mode_event(*, game: bool) -> Envelope:
    return Envelope(
        kind=Kind.EVENT,
        name=E.SYSTEM_MODE_CHANGED,
        src=Source(role="core", id="core"),
        payload={"previous": "idle", "current": "x", "game_running": game},
    )


def make_worker(**performance: Any) -> tuple[VoiceWorker, list[str]]:
    config = VoiceConfig.model_validate({"performance": performance})
    worker = VoiceWorker(
        client=FakeIpcClient(),
        service="voice",
        pipeline_factory=spy_factory,
        default_config=config,
    )
    applied: list[str] = []
    worker.priority_setter = lambda level: applied.append(level) is None
    return worker, applied


async def test_a_game_in_the_foreground_lowers_the_priority_and_leaving_it_restores_it() -> None:
    worker, applied = make_worker(priority="normal", game_priority="below_normal")
    await worker._on_mode_changed(mode_event(game=True))
    await worker._on_mode_changed(mode_event(game=False))
    assert applied == ["below_normal", "normal"]


async def test_the_configured_levels_are_the_ones_applied() -> None:
    worker, applied = make_worker(priority="above_normal", game_priority="normal")
    await worker._on_mode_changed(mode_event(game=True))
    await worker._on_mode_changed(mode_event(game=False))
    assert applied == ["normal", "above_normal"]


async def test_a_mode_change_without_the_game_flag_means_no_game() -> None:
    worker, applied = make_worker()
    env = mode_event(game=True)
    env.payload.pop("game_running")
    await worker._on_mode_changed(env)
    assert applied == ["normal"]


def test_the_worker_listens_for_mode_changes() -> None:
    client = FakeIpcClient()
    worker = VoiceWorker(client=client, service="voice", pipeline_factory=spy_factory)
    worker._register_handlers()
    assert E.SYSTEM_MODE_CHANGED in [name for name, _ in client.event_handlers]


def test_the_default_priority_is_normal_and_a_game_gets_less() -> None:
    perf = VoiceConfig().performance
    assert (perf.priority, perf.game_priority, perf.cpu_threads) == (
        "normal",
        "below_normal",
        0,
    )


@pytest.mark.parametrize(
    "bad", [{"priority": "realtime"}, {"game_priority": "high"}, {"cpu_threads": -1}]
)
def test_nonsense_in_the_performance_section_is_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        VoiceConfig.model_validate({"performance": bad})


def test_the_configured_thread_count_reaches_the_speech_engine() -> None:
    pytest.importorskip("faster_whisper")
    config = VoiceConfig.model_validate({"performance": {"cpu_threads": 3}})
    stt = build_components(config, service="voice")["stt"]
    assert stt.cpu_threads == 3
    auto = build_components(VoiceConfig(), service="voice")["stt"]
    assert 1 <= auto.cpu_threads <= 8


class FakeProcess:
    classes: list[int] = []

    def nice(self, value: int) -> None:
        FakeProcess.classes.append(value)


class FakePsutil:
    BELOW_NORMAL_PRIORITY_CLASS = 1
    NORMAL_PRIORITY_CLASS = 2
    ABOVE_NORMAL_PRIORITY_CLASS = 3
    Process = FakeProcess


def test_apply_priority_sets_the_matching_windows_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "psutil", FakePsutil)
    FakeProcess.classes = []
    for level in ("below_normal", "normal", "above_normal"):
        assert apply_priority(level) is True
    assert FakeProcess.classes == [1, 2, 3]


def test_apply_priority_does_nothing_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert apply_priority("normal") is False


def test_apply_priority_survives_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing(FakePsutil):
        class Process:
            def nice(self, value: int) -> None:
                raise PermissionError("denied")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "psutil", Failing)
    assert apply_priority("below_normal") is False
    assert hasattr(worker_main, "apply_priority")
