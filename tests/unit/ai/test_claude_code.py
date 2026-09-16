"""ClaudeCodeProvider: stream parser (recorded fixture), args, fake CLI, cancel, errors."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import psutil
import pytest

from nox.ai.base import AiRole, Message
from nox.ai.config import ClaudeCodeConfig
from nox.ai.errors import ProviderError, ProviderTimeoutError, ProviderUnavailableError
from nox.ai.providers.claude_code import ClaudeCodeProvider, ClaudeStreamParser, render_prompt
from nox.core.events import HealthStatus

from .conftest import make_request

FIXTURES = Path(__file__).parent / "fixtures"

# Inline minimal sample (shape recorded from Claude Code 2.1.266, identifiers redacted).
INIT = {"type": "system", "subtype": "init", "model": "claude-haiku-4-5", "session_id": "s"}
DELTA_A = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Hal"},
    },
}
DELTA_B = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "lo"},
    },
}
ASSISTANT = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "Hallo"}]},
}
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Hallo",
    "duration_ms": 1200,
    "duration_api_ms": 900,
    "total_cost_usd": 0.0051,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 100,
        "output_tokens": 4,
    },
}
LOGGED_OUT = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "terminal_reason": "api_error",
    "result": "Not logged in · Please run /login",
    "usage": {},
}


def feed_all(lines: list[dict[str, object]]) -> list[tuple[str, str]]:
    parser = ClaudeStreamParser()
    out = []
    for line in lines:
        item = parser.feed(json.dumps(line))
        if item is not None:
            out.append((item.kind, item.text))
    return out


def test_parser_streams_deltas_and_dedupes_assistant_repeat() -> None:
    kinds = feed_all([INIT, DELTA_A, DELTA_B, ASSISTANT, RESULT])
    assert kinds[0] == ("init", "")
    assert [k for k in kinds if k[0] == "delta"] == [("delta", "Hal"), ("delta", "lo")]
    assert ("text", "Hallo") in kinds  # repeat is reported as text, never as delta


def test_parser_uses_assistant_text_without_partial_messages() -> None:
    kinds = feed_all([INIT, ASSISTANT, RESULT])
    assert ("delta", "Hallo") in kinds


def test_parser_result_usage_cost_and_error() -> None:
    parser = ClaudeStreamParser()
    parser.feed(json.dumps(INIT))
    item = parser.feed(json.dumps(RESULT))
    assert item is not None and item.kind == "result"
    assert item.tokens_in == 402 and item.tokens_out == 4
    assert item.cost_usd == 0.0051 and item.duration_ms == 1200
    assert item.model == "claude-haiku-4-5" and item.is_error is False
    error = ClaudeStreamParser().feed(json.dumps(LOGGED_OUT))
    assert error is not None and error.is_error and "Not logged in" in error.error


def test_parser_tolerates_garbage_and_blank_lines() -> None:
    parser = ClaudeStreamParser()
    assert parser.feed("   ") is None
    item = parser.feed("not json")
    assert item is not None and item.kind == "other"
    item = parser.feed("[1, 2]")
    assert item is not None and item.kind == "other"


@pytest.mark.parametrize("name", ["claude_stream_sample.ndjson", "claude_stream_logged_out.ndjson"])
def test_parser_on_recorded_fixture(name: str) -> None:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixture {name} not recorded yet (run spikes/sp01_claude_code.py)")
    parser = ClaudeStreamParser()
    items = [i for i in (parser.feed(ln) for ln in path.read_text("utf-8").splitlines()) if i]
    results = [i for i in items if i.kind == "result"]
    assert len(results) == 1
    text = "".join(i.text for i in items if i.kind == "delta")
    if name.endswith("logged_out.ndjson"):
        assert results[0].is_error and "logged in" in results[0].error.lower()
    else:
        assert not results[0].is_error
        assert text.strip() == results[0].text.strip()
        assert results[0].tokens_out and results[0].cost_usd is not None
    raw = path.read_text("utf-8")
    for secret_marker in ("sk-ant", "Bearer ", "\\\\Users\\\\", ":/Users/"):
        assert secret_marker not in raw


def test_build_args_flags_model_system_prompt_and_no_secrets() -> None:
    p = ClaudeCodeProvider(ClaudeCodeConfig(model="sonnet", max_budget_usd=0.05))
    args = p.build_args("SYS")
    joined = " ".join(args)
    for flag in (
        "-p",
        "--output-format stream-json",
        "--verbose",
        "--include-partial-messages",
        "--no-session-persistence",
        "--max-turns 1",
        "--permission-prompts none",
        "--safe-mode",
        "--model sonnet",
        "--max-budget-usd 0.0500",
        "--system-prompt SYS",
    ):
        assert flag in joined
    assert "--tools" in args and args[args.index("--tools") + 1] == ""
    assert "--bare" not in args and "--dangerously-skip-permissions" not in args
    assert not any("key" in a.lower() or "token" in a.lower() for a in args)
    p2 = ClaudeCodeProvider(ClaudeCodeConfig(safe_mode=False))
    args2 = p2.build_args("", model="haiku")
    assert "--safe-mode" not in args2 and "--system-prompt" not in args2
    assert args2[args2.index("--model") + 1] == "haiku"


def test_render_prompt_multi_turn() -> None:
    request = make_request("Und jetzt?", system="SYS")
    request.messages[1:1] = [
        Message(role="user", content="Hallo"),
        Message(role="assistant", content="Hi"),
    ]
    system, prompt = render_prompt(request)
    assert system == "SYS"
    assert "User: Hallo" in prompt and "Nox: Hi" in prompt
    assert prompt.endswith("Current message from the user:\nUnd jetzt?")
    assert render_prompt(make_request("x")) == ("", "x")


# ---- fake CLI subprocess -----------------------------------------------------------------------

FAKE_CLI = r"""
import json, os, sys, time
args = sys.argv[1:]
if "--version" in args:
    print("9.9.9 (fake)"); sys.exit(0)
mode = os.environ.get("FAKE_CLI_MODE", "ok")
prompt = sys.stdin.read()
pidfile = os.environ.get("FAKE_CLI_PIDFILE")
if pidfile:
    open(pidfile, "w").write(str(os.getpid()))
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
emit({"type": "system", "subtype": "init", "model": "fake-model", "session_id": "s"})
if mode == "logged_out":
    emit({"type": "result", "subtype": "success", "is_error": True,
          "result": "Not logged in · Please run /login", "usage": {}}); sys.exit(1)
if mode == "crash":
    sys.stderr.write("segfault-ish\n"); sys.exit(3)
for piece in ["Echo: ", prompt.strip()[:20]]:
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
          "delta": {"type": "text_delta", "text": piece}}})
    if mode == "hang":
        time.sleep(30)
    time.sleep(0.02)
emit({"type": "result", "subtype": "success", "is_error": False, "result": "Echo: x",
      "total_cost_usd": 0.001, "duration_ms": 50,
      "usage": {"input_tokens": 3, "output_tokens": 2}})
"""


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    return script


def fake_provider(script: Path, env: dict[str, str], **cfg: object) -> ClaudeCodeProvider:
    import os

    full_env = {**os.environ, "PYTHONIOENCODING": "utf-8", **env}
    return ClaudeCodeProvider(
        ClaudeCodeConfig(**cfg),  # type: ignore[arg-type]
        env=full_env,
        command_override=[sys.executable, str(script)],
    )


async def test_subprocess_stream_and_complete(fake_cli: Path) -> None:
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "ok"})
    chunks = [c async for c in p.stream(make_request("Hallo Nox", system="S"))]
    assert "".join(c.delta for c in chunks) == "Echo: Hallo Nox"
    assert chunks[-1].done is True
    last = p.last_response("req-1")
    assert last is not None and last.tokens_in == 3 and last.cost_usd == 0.001
    response = await p.complete(make_request("Hallo Nox", request_id="r2"))
    assert response.text == "Echo: Hallo Nox" and response.provider == "claude_code"
    assert response.degraded is False


async def test_subprocess_logged_out_is_unavailable(fake_cli: Path) -> None:
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "logged_out"})
    with pytest.raises(ProviderUnavailableError, match="logged in"):
        await p.complete(make_request())


async def test_subprocess_crash_without_result_is_provider_error(fake_cli: Path) -> None:
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "crash"})
    with pytest.raises(ProviderError, match="no result line"):
        await p.complete(make_request())


async def test_subprocess_timeout_kills_child(fake_cli: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "hang", "FAKE_CLI_PIDFILE": str(pidfile)})
    with pytest.raises(ProviderTimeoutError):
        await p.complete(make_request(timeout_s=0.8))
    pid = int(pidfile.read_text())
    await asyncio.sleep(0.3)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


async def test_cancel_kills_child(fake_cli: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "hang", "FAKE_CLI_PIDFILE": str(pidfile)})
    received: list[str] = []

    async def consume() -> None:
        async for chunk in p.stream(make_request(timeout_s=60)):
            received.append(chunk.delta)

    task = asyncio.create_task(consume())
    for _ in range(100):
        await asyncio.sleep(0.05)
        if received:
            break
    assert received == ["Echo: "]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pid = int(pidfile.read_text())
    await asyncio.sleep(0.3)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


async def test_health_with_fake_cli(fake_cli: Path) -> None:
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "ok"})
    info = await p.health()
    assert info.status is HealthStatus.AVAILABLE and p.version == "9.9.9 (fake)"
    p2 = fake_provider(fake_cli, {"FAKE_CLI_MODE": "logged_out"})
    info2 = await p2.health()
    assert info2.status is HealthStatus.UNAVAILABLE and "logged in" in info2.reason
    p3 = fake_provider(fake_cli, {"FAKE_CLI_MODE": "ok"}, health_roundtrip=False)
    assert (await p3.health()).status is HealthStatus.LIMITED


async def test_missing_command_is_unavailable() -> None:
    p = ClaudeCodeProvider(ClaudeCodeConfig(command="definitely-not-a-real-cli-xyz"))
    info = await p.health()
    assert info.status is HealthStatus.UNAVAILABLE and "not found" in info.reason
    with pytest.raises(ProviderUnavailableError):
        await p.complete(make_request())


async def test_disabled_provider() -> None:
    p = ClaudeCodeProvider(ClaudeCodeConfig(enabled=False))
    assert (await p.health()).status is HealthStatus.UNAVAILABLE
    with pytest.raises(ProviderUnavailableError):
        await p.complete(make_request())


async def test_empty_prompt_rejected(fake_cli: Path) -> None:
    p = fake_provider(fake_cli, {})
    request = make_request("   ")
    with pytest.raises(ProviderError, match="empty prompt"):
        await p.complete(request)


@pytest.mark.network
@pytest.mark.spike
async def test_real_cli_roundtrip() -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not installed")
    p = ClaudeCodeProvider(ClaudeCodeConfig(model="haiku"))
    info = await p.health()
    if info.status is not HealthStatus.AVAILABLE:
        pytest.skip(f"claude not usable: {info.reason}")
    response = await p.complete(
        make_request("Reply with exactly: OK", role=AiRole.CHAT, timeout_s=60)
    )
    assert "OK" in response.text
