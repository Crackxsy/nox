"""nox.supervisor.messages: envelope encode/decode, token file, env settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.ipc.protocol import Kind, Source
from nox.supervisor import messages as m


def test_roundtrip() -> None:
    env = m.make(m.NAME_KILL, {"reason": "x"}, Source(role="supervisor", id="s"), kind=Kind.REQUEST)
    line = m.encode(env)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    back = m.decode(line)
    assert back.name == m.NAME_KILL and back.id == env.id and back.payload == {"reason": "x"}
    assert back.src.role == "supervisor"


@pytest.mark.parametrize("raw", [b"not json\n", b"[1,2]\n", b'{"name": "sup.kill"}\n'])
def test_decode_errors(raw: bytes) -> None:
    with pytest.raises(m.ProtocolError):
        m.decode(raw)


def test_token_file(tmp_path: Path) -> None:
    m.token_path(tmp_path).write_text("a" * 32, encoding="utf-8")
    assert m.read_token(tmp_path) == "a" * 32
    m.token_path(tmp_path).write_text("short", encoding="utf-8")
    with pytest.raises(ValueError):
        m.read_token(tmp_path)


def test_env_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(m.ENV_PORT, raising=False)
    monkeypatch.delenv(m.ENV_TOKEN, raising=False)
    assert m.env_settings() is None
    monkeypatch.setenv(m.ENV_PORT, "4711")
    monkeypatch.setenv(m.ENV_TOKEN, "t" * 32)
    assert m.env_settings() == ("127.0.0.1", 4711, "t" * 32)
