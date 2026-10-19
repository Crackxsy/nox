"""The CI guard of Security Model §10, run locally: input synthesis lives in exactly two files,
both refuse while a game runs, and the automation / process-memory APIs appear nowhere."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ALLOWED = {"src/nox/desktop/keyboard.py", "src/nox/desktop/computer_use.py"}
FORBIDDEN = re.compile("|".join(["pydirect" + "input", "pyauto" + "gui", "ReadProcess" + "Memory",
                                 "WriteProcess" + "Memory"]))  # fmt: skip


def _sources() -> list[Path]:
    return [*ROOT.joinpath("src").rglob("*.py"), *ROOT.joinpath("plugins", "rl").rglob("*.py")]


def test_send_input_only_in_the_two_guarded_files() -> None:
    found = {
        p.relative_to(ROOT).as_posix()
        for p in _sources()
        if "SendInput" in p.read_text(encoding="utf-8", errors="replace")
    }
    assert found == ALLOWED


def test_both_files_keep_their_game_guard() -> None:
    for name in ALLOWED:
        text = ROOT.joinpath(name).read_text(encoding="utf-8")
        assert "GameRunningError" in text, name
        assert "if game_running:" in text, name


def test_no_automation_or_process_memory_api_anywhere() -> None:
    hits = [
        p.relative_to(ROOT).as_posix()
        for p in _sources()
        if FORBIDDEN.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert hits == []


def test_the_ci_workflow_names_both_files() -> None:
    workflow = ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
    for name in ALLOWED:
        assert name in workflow
