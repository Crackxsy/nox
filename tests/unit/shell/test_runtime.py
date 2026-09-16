from __future__ import annotations

import json
from pathlib import Path

from nox.shell.runtime import (
    ShellState,
    load_config,
    load_shell_state,
    read_ipc_endpoints,
    read_session_token,
    read_supervisor_token,
    resolve_runtime_dir,
    save_shell_state,
)


def test_tokens_missing_or_empty_are_none(tmp_path: Path) -> None:
    assert read_session_token(tmp_path) is None
    (tmp_path / "session.token").write_text("   \n", encoding="utf-8")
    assert read_session_token(tmp_path) is None
    (tmp_path / "session.token").write_text("abcdefghijklmnop1234\n", encoding="utf-8")
    assert read_session_token(tmp_path) == "abcdefghijklmnop1234"
    (tmp_path / "supervisor.token").write_text("sup-token", encoding="utf-8")
    assert read_supervisor_token(tmp_path) == "sup-token"


def test_ipc_endpoints_parse_and_reject_garbage(tmp_path: Path) -> None:
    assert read_ipc_endpoints(tmp_path) is None
    (tmp_path / "ipc.json").write_text("{not json", encoding="utf-8")
    assert read_ipc_endpoints(tmp_path) is None
    (tmp_path / "ipc.json").write_text(json.dumps({"ws_port": 0, "http_port": 1}), encoding="utf-8")
    assert read_ipc_endpoints(tmp_path) is None
    (tmp_path / "ipc.json").write_text(
        json.dumps({"ws_port": 47800, "http_port": 47801}), encoding="utf-8"
    )
    ep = read_ipc_endpoints(tmp_path)
    assert ep is not None
    assert (ep.ws_port, ep.http_port, ep.host) == (47800, 47801, "127.0.0.1")
    assert ep.supervisor_port == 47799


def test_shell_state_round_trip(tmp_path: Path) -> None:
    assert load_shell_state(tmp_path) == ShellState()
    save_shell_state(tmp_path / "sub", ShellState(x=10, y=20, visible=False, click_through=True))
    loaded = load_shell_state(tmp_path / "sub")
    assert (loaded.x, loaded.y, loaded.visible, loaded.click_through) == (10, 20, False, True)
    assert not (tmp_path / "sub" / "shell.json.tmp").exists()
    (tmp_path / "sub" / "shell.json").write_text("garbage", encoding="utf-8")
    assert load_shell_state(tmp_path / "sub") == ShellState()


def test_resolve_runtime_dir_prefers_dir_with_token(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    b.mkdir()
    (b / "session.token").write_text("t", encoding="utf-8")
    assert resolve_runtime_dir([a, b]) == b
    assert resolve_runtime_dir([a, tmp_path / "c"]) == a


def test_load_config(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("voice:\n  stt:\n    push_to_talk_hotkey: ctrl+alt+space\n", encoding="utf-8")
    assert load_config(cfg)["voice"]["stt"]["push_to_talk_hotkey"] == "ctrl+alt+space"  # type: ignore[index]
    cfg.write_text("- just\n- a list\n", encoding="utf-8")
    assert load_config(cfg) == {}
    cfg.write_text("a: [unclosed", encoding="utf-8")
    assert load_config(cfg) == {}
    # repo defaults are found without an explicit path
    assert "ipc" in load_config()
