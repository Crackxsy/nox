"""Vision Stage 2, end to end (Spec v0.9 EPIC-18): a real `NoxCore` (headless, no plugin
subprocess - the confidence-gating/budget-guard themselves are already covered directly against
`FrameSampler` in `tests/unit/plugins/rl/vision/test_sampler.py`). `NoxCore.start()` already calls
`nox.rl.install.install(core)` itself (`app.py`'s generic `nox.<name>.install` release-extension
loader, step 11b - unconditionally enabled for `rl`), which now also wires `nox.rl.vision.
install_vision` (guarded) - these tests must NOT call `install(core)` again themselves, or every
service ends up double-subscribed (confirmed the hard way: an earlier draft of this file did, and
every match/frame/analysis row showed up twice). Publishes the events a real `rl` plugin worker's
vision loop would have emitted, and exercises migration `0009_vision.sql`, the frame-persistence
service, and the post-match analysis + coaching-speech wiring together. Modelled on
`tests/integration/test_clips.py`."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.events import E, Event
from nox.data.rl_vision_repos import RlVisionAnalysisRepository, RlVisionFrameRepository
from nox.voice.base import TtsRequest

pytestmark = pytest.mark.timeout(60)


def _config(tmp_path: Path, *, vision_enabled: bool = True):
    base = random.randint(20000, 60000)  # noqa: S311
    (tmp_path / "vault").mkdir()
    overrides = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(tmp_path / "vault"),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": "balanced"},
        "security": {"profile": "companion"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": []},  # no plugin subprocess - events are published directly
        "rl": {"vision": {"enabled": vision_enabled, "backend": "opencv"}},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


def _capture_speech(core: NoxCore) -> list[TtsRequest]:
    """Swaps `.say` on the real speaker object `install_vision`/`install` already captured a
    reference to at boot (step 11b runs inside `core.start()`, before this fixture ever sees
    `core`) - mutating the instance in place, rather than replacing `core.speaker` itself, is what
    makes the already-constructed services observe the fake."""
    said: list[TtsRequest] = []

    async def _fake_say(request: TtsRequest) -> None:
        said.append(request)

    core.speaker.say = _fake_say  # type: ignore[method-assign]
    return said


@pytest.fixture
async def core(tmp_path: Path):
    cfg = _config(tmp_path)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
    await core.start()
    try:
        yield core
    finally:
        await core.stop()


async def test_migration_0009_vision_tables_exist(core: NoxCore) -> None:
    rows = core.db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'rl_vision_%'"
    )
    names = {r["name"] for r in rows}
    assert names == {"rl_vision_frames", "rl_vision_analysis"}


async def test_extension_boot_wires_vision_alongside_stage1(core: NoxCore) -> None:
    # `nox.rl.install.install` returns `None` on success (matches every other `nox.<name>.install`
    # module), so `core.extensions["rl"]` is `None` on BOTH success and a top-level failure -
    # `core.rl_persistence`/`core.rl_vision_*` (set only on success) are the real signal.
    assert "rl" in core.extensions
    assert core.rl_persistence is not None  # type: ignore[attr-defined]
    assert core.rl_vision_persistence is not None  # type: ignore[attr-defined]
    assert core.rl_vision_analysis is not None  # type: ignore[attr-defined]


async def test_detections_persist_and_post_match_analysis_fires_a_short_coaching_line(
    core: NoxCore,
) -> None:
    said = _capture_speech(core)
    seen: list[Event] = []
    core.bus.subscribe(E.RL_VISION_ANALYSIS, lambda ev: seen.append(ev))

    await core.bus.publish(Event(name=E.RL_MATCH_STARTED, payload={"match_id": 1}))
    await core.bus.publish(
        Event(
            name=E.RL_VISION_DETECTIONS,
            payload={
                "backend": "opencv",
                "detections": [
                    {"entity": "ball", "confidence": 0.7, "x": 0.2, "y": 0.5, "w": 0.02, "h": 0.02}
                ],
            },
        )
    )
    await core.bus.publish(
        Event(
            name=E.RL_MATCH_ENDED,
            payload={"match_id": 1, "result": "win", "summary_short": "Win 3-1."},
        )
    )

    frames_repo = RlVisionFrameRepository(core.db)
    analysis_repo = RlVisionAnalysisRepository(core.db)
    match_id = seen[0].payload["match_id"] if seen else None
    assert match_id is not None
    frames = frames_repo.list_for_match(match_id)
    assert len(frames) == 1
    assert frames[0].entity == "ball"

    assert len(seen) == 1
    assert seen[0].payload["frames_analyzed"] == 1
    analyses = analysis_repo.list_for_match(match_id)
    assert len(analyses) == 1
    assert analyses[0].coaching_summary
    assert any(m.text == analyses[0].coaching_summary for m in said)


async def test_no_detections_no_match_means_no_fabricated_analysis(core: NoxCore) -> None:
    said = _capture_speech(core)
    seen: list[Event] = []
    core.bus.subscribe(E.RL_VISION_ANALYSIS, lambda ev: seen.append(ev))

    await core.bus.publish(Event(name=E.RL_MATCH_STARTED, payload={"match_id": 1}))
    await core.bus.publish(
        Event(
            name=E.RL_MATCH_ENDED,
            payload={"match_id": 1, "result": "loss", "summary_short": "Loss 1-2."},
        )
    )

    assert seen == []
    # `said` still gets Stage 1's own always-on `RlMatchAnnouncer` short summary ("Loss 1-2.") -
    # unrelated to vision, so the real assertion is that vision contributed nothing to it.
    assert all(m.text != "" and "Rough vision read" not in m.text for m in said)
    analysis_repo = RlVisionAnalysisRepository(core.db)
    assert analysis_repo.list_for_match(1) == []  # matches this DB's fresh autoincrement


async def test_vision_install_failure_never_breaks_stage1_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 11b's own guard (`nox.rl.install.install`'s try/except around `install_vision`) plus
    `app.py`'s outer per-extension try/except (Spec/ENGINEERING.md hard rule: additive code must
    never break an existing feature) - a simulated Vision Stage 2 wiring failure still leaves
    Stage 1 fully wired. Patched before `core.start()`, since step 11b runs inside it."""
    import nox.rl.vision as vision_module

    def _boom(_core: Any) -> None:
        raise RuntimeError("simulated vision wiring failure")

    monkeypatch.setattr(vision_module, "install_vision", _boom)

    cfg = _config(tmp_path)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
    await core.start()  # must not raise despite the simulated failure
    try:
        assert "rl" in core.extensions
        assert core.rl_persistence is not None  # type: ignore[attr-defined]
        assert not hasattr(core, "rl_vision_persistence")
    finally:
        await core.stop()
