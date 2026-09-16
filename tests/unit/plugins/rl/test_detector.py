"""Game detection (ST-12-01): process-list polling only, no window handle/memory API."""

from __future__ import annotations

from nox_plugin_rl.detector import GameDetector


def test_detects_a_configured_process_name_case_insensitively() -> None:
    detector = GameDetector(
        ["RocketLeague.exe"], list_process_names=lambda: ["explorer.exe", "rocketleague.exe"]
    )
    assert detector.poll() is True
    assert detector.running is True


def test_reports_not_running_when_absent() -> None:
    detector = GameDetector(["RocketLeague.exe"], list_process_names=lambda: ["explorer.exe"])
    assert detector.poll() is False
    assert detector.running is False


def test_tracks_transition_from_running_to_not_running() -> None:
    names = ["RocketLeague.exe"]
    state = {"running": True}
    detector = GameDetector(
        names, list_process_names=lambda: ["RocketLeague.exe"] if state["running"] else []
    )
    assert detector.poll() is True
    state["running"] = False
    assert detector.poll() is False
