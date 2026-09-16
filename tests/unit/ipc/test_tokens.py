"""Session/worker token behaviour: file ACL, one-time consumption, TTL, constant-time compare."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nox.ipc.tokens import (
    SESSION_TOKEN_FILE,
    WORKER_TOKEN_ENV,
    TokenStore,
    constant_time_equals,
    generate_token,
    read_session_token,
)


def test_generate_token_is_long_and_unique() -> None:
    a, b = generate_token(), generate_token()
    assert a != b
    assert len(a) >= 40  # 32 bytes url-safe base64


def test_constant_time_equals() -> None:
    assert constant_time_equals("abc", "abc")
    assert not constant_time_equals("abc", "abd")
    assert not constant_time_equals("abc", "abcd")


def test_session_token_written_and_readable(tmp_path: Path) -> None:
    store = TokenStore()
    path = store.write_session_token(tmp_path / "runtime")
    assert path.name == SESSION_TOKEN_FILE
    assert read_session_token(tmp_path / "runtime") == store.session_token
    assert store.session_file_restricted is True
    store.remove_session_token()
    assert not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="icacls ACL check is Windows-only")
def test_session_token_file_acl_is_owner_only(tmp_path: Path) -> None:
    store = TokenStore()
    path = store.write_session_token(tmp_path / "runtime")
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    assert "BUILTIN\\Users" not in out
    assert "Everyone" not in out
    assert "Authenticated Users" not in out
    assert "(I)" not in out  # nothing inherited


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_session_token_file_mode_posix(tmp_path: Path) -> None:
    store = TokenStore()
    path = store.write_session_token(tmp_path / "runtime")
    assert path.stat().st_mode & 0o777 == 0o600


def test_read_session_token_rejects_short_file(tmp_path: Path) -> None:
    (tmp_path / SESSION_TOKEN_FILE).write_text("short")
    with pytest.raises(ValueError):
        read_session_token(tmp_path)


def test_session_roles_use_session_token() -> None:
    store = TokenStore()
    for role in ("shell", "pet", "dashboard"):
        assert store.authenticate(store.session_token, role, f"{role}:1").ok
        assert not store.authenticate(generate_token(), role, f"{role}:1").ok


def test_worker_cannot_use_session_token_and_shell_cannot_use_worker_token() -> None:
    store = TokenStore()
    wt = store.issue_worker_token("worker:stt")
    assert not store.authenticate(store.session_token, "worker", "worker:stt").ok
    assert not store.authenticate(wt, "shell", "shell:1").ok
    # the failed shell attempt must not have consumed the worker token
    assert store.authenticate(wt, "worker", "worker:stt").ok


def test_worker_token_is_one_time() -> None:
    store = TokenStore()
    wt = store.issue_worker_token("worker:tts")
    assert store.outstanding_worker_tokens() == 1
    first = store.authenticate(wt, "worker", "worker:tts")
    assert first.ok and first.kind == "worker"
    second = store.authenticate(wt, "worker", "worker:tts")
    assert not second.ok
    assert store.outstanding_worker_tokens() == 0


def test_worker_token_bound_to_worker_id() -> None:
    store = TokenStore()
    wt = store.issue_worker_token("worker:stt")
    decision = store.authenticate(wt, "worker", "plugin:evil")
    assert not decision.ok
    assert "different worker" in decision.reason
    # a mismatch consumes the token as well (no second try)
    assert not store.authenticate(wt, "worker", "worker:stt").ok


def test_worker_token_expires() -> None:
    now = [1000.0]
    store = TokenStore(worker_ttl_s=5.0, clock=lambda: now[0])
    wt = store.issue_worker_token("worker:stt")
    now[0] += 6.0
    decision = store.authenticate(wt, "worker", "worker:stt")
    assert not decision.ok
    assert store.purge_expired() == 0  # already removed by the failed attempt
    wt2 = store.issue_worker_token("worker:stt", ttl_s=1.0)
    now[0] += 2.0
    assert store.purge_expired() == 1
    assert not store.authenticate(wt2, "worker", "worker:stt").ok


def test_worker_env_carries_token_only_in_env() -> None:
    store = TokenStore()
    env = store.worker_env("plugin:x")
    assert set(env) == {WORKER_TOKEN_ENV}
    assert store.authenticate(env[WORKER_TOKEN_ENV], "plugin", "plugin:x").ok


def test_unknown_roles_never_authenticate() -> None:
    store = TokenStore()
    for role in ("core", "supervisor", "remote", "bogus"):
        assert not store.authenticate(store.session_token, role, "x").ok
