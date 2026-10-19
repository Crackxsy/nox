"""The CI guard of Security Model §10, run locally (`scripts/check_input_synthesis.py`): input
synthesis lives in exactly two game-guarded files, across src/ and every plugin, and the
automation / process-memory APIs appear nowhere."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]


def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_input_synthesis", ROOT / "scripts" / "check_input_synthesis.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_repository_is_clean() -> None:
    assert guard().problems() == []


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel in guard().ALLOWED:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    (tmp_path / "plugins" / "demo").mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize(
    "line",
    [
        "user32." + "keybd" + "_event(0x41, 0, 0, 0)",
        "user32." + "mouse" + "_event(2, 0, 0, 0, 0)",
        "user32." + "SetCursor" + "Pos(10, 10)",
        "user32." + "Send" + "Input(1, ev, size)",
        "user32.PostMessageW(hwnd, WM_" + "KEYDOWN, 0x41, 0)",
        "user32.SendMessageW(hwnd, WM_" + "LBUTTONDOWN, 1, 0)",
    ],
)
def test_synthesis_in_any_plugin_is_found(tree: Path, line: str) -> None:
    (tree / "plugins" / "demo" / "evil.py").write_text(line + "\n", encoding="utf-8")
    assert any("plugins/demo/evil.py" in p for p in guard().problems(tree))


def test_a_close_message_and_a_chat_model_name_are_not_synthesis(tree: Path) -> None:
    (tree / "plugins" / "demo" / "ok.py").write_text(
        "user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)\nclass Chat" + "Send" + "Input: ...\n",
        encoding="utf-8",
    )
    assert guard().problems(tree) == []


def test_both_allowed_files_keep_their_game_guard(tree: Path) -> None:
    path = tree / guard().ALLOWED[1]
    text = path.read_text(encoding="utf-8").replace("if game_running:", "if True:")
    path.write_text(text, encoding="utf-8")
    assert any("game guard" in p for p in guard().problems(tree))


def test_the_ci_workflow_runs_the_guard() -> None:
    workflow = ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
    assert "scripts/check_input_synthesis.py" in workflow
