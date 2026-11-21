"""ClaudeCodeProvider: stream parser (recorded fixture), args, fake CLI, cancel, errors."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
import pytest

from nox.ai.base import AiRole, Message
from nox.ai.config import ClaudeCodeConfig
from nox.ai.errors import (
    ProviderError,
    ProviderLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
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
calls = os.environ.get("FAKE_CLI_CALLS")
if calls:
    with open(calls, "a") as fh:
        fh.write(" ".join(args[:2]) + "\n")
if "--version" in args:
    print("9.9.9 (fake)"); sys.exit(0)
if args[:2] == ["auth", "status"]:
    auth = os.environ.get("FAKE_CLI_AUTH", "in")
    if auth == "unsupported":
        sys.stderr.write("unknown command: auth\n"); sys.exit(1)
    print(json.dumps({"loggedIn": auth == "in"})); sys.exit(0 if auth == "in" else 1)
mode_file = os.environ.get("FAKE_CLI_MODE_FILE")
mode = open(mode_file).read().strip() if mode_file else os.environ.get("FAKE_CLI_MODE", "ok")
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
def refuse(with_event):
    if with_event:
        emit({"type": "rate_limit_event", "rate_limit_info": {
              "status": "rejected", "rateLimitType": "five_hour",
              "resetsAt": float(os.environ.get("FAKE_CLI_RESET", "0"))}})
    sentence = "You've hit your limit · resets 11pm (Europe/Berlin)"
    emit({"type": "assistant", "message": {"model": "<synthetic>", "role": "assistant",
          "content": [{"type": "text", "text": sentence}]},
          "error": "rate_limit", "isApiErrorMessage": True})
    emit({"type": "result", "subtype": "success", "is_error": True,
          "result": sentence, "usage": {}}); sys.exit(1)
if mode == "limit":
    refuse(True)
if mode == "limit_text":
    refuse(False)
for piece in ["Echo: ", prompt.strip()[:20]]:
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
          "delta": {"type": "text_delta", "text": piece}}})
    if mode == "hang":
        time.sleep(30)
    if mode == "limit_late":
        refuse(True)
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


def fake_provider(
    script: Path,
    env: dict[str, str],
    *,
    now: Callable[[], datetime] | None = None,
    **cfg: object,
) -> ClaudeCodeProvider:
    import os

    full_env = {**os.environ, "PYTHONIOENCODING": "utf-8", **env}
    return ClaudeCodeProvider(
        ClaudeCodeConfig(**cfg),  # type: ignore[arg-type]
        env=full_env,
        command_override=[sys.executable, str(script)],
        now=now,
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


async def test_the_opt_in_roundtrip_probe_verifies_the_login_with_a_real_request(
    fake_cli: Path,
) -> None:
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "ok"}, health_roundtrip=True)
    info = await p.health()
    assert info.status is HealthStatus.AVAILABLE and p.version == "9.9.9 (fake)"
    assert "round-trip" in info.reason
    p2 = fake_provider(fake_cli, {"FAKE_CLI_MODE": "logged_out"}, health_roundtrip=True)
    info2 = await p2.health()
    assert info2.status is HealthStatus.UNAVAILABLE and "logged in" in info2.reason


def calls_of(path: Path) -> list[str]:
    return path.read_text("utf-8").splitlines() if path.exists() else []


async def test_health_asks_no_model_and_spawns_nothing_once_checked(
    fake_cli: Path, tmp_path: Path
) -> None:
    calls, pidfile = tmp_path / "calls", tmp_path / "pid"
    env = {"FAKE_CLI_CALLS": str(calls), "FAKE_CLI_AUTH": "in", "FAKE_CLI_PIDFILE": str(pidfile)}
    p = fake_provider(fake_cli, env)

    first = await p.health()
    again = await p.health()

    assert first.status is HealthStatus.AVAILABLE and "signed in" in first.reason
    assert again.status is HealthStatus.AVAILABLE and p.version == "9.9.9 (fake)"
    assert calls_of(calls) == ["--version", "auth status"]  # once, and neither asks the model
    assert not pidfile.exists()


async def test_a_signed_out_account_is_unavailable_without_a_model_call(
    fake_cli: Path, tmp_path: Path
) -> None:
    pidfile = tmp_path / "pid"
    p = fake_provider(fake_cli, {"FAKE_CLI_AUTH": "out", "FAKE_CLI_PIDFILE": str(pidfile)})

    info = await p.health()

    assert info.status is HealthStatus.UNAVAILABLE and "not signed in" in info.reason
    assert not pidfile.exists()


async def test_a_cli_without_auth_status_is_limited_not_guessed_available(fake_cli: Path) -> None:
    p = fake_provider(fake_cli, {"FAKE_CLI_AUTH": "unsupported"})

    info = await p.health()

    assert info.status is HealthStatus.LIMITED and "login not verified" in info.reason


async def test_an_answered_request_makes_health_available_without_another_check(
    fake_cli: Path, tmp_path: Path
) -> None:
    calls = tmp_path / "calls"
    p = fake_provider(fake_cli, {"FAKE_CLI_CALLS": str(calls), "FAKE_CLI_AUTH": "unsupported"})
    assert (await p.health()).status is HealthStatus.LIMITED

    await p.complete(make_request("Hallo"))
    seen = len(calls_of(calls))
    info = await p.health()

    assert info.status is HealthStatus.AVAILABLE and "answering" in info.reason
    assert len(calls_of(calls)) == seen


async def test_a_failed_request_is_limited_and_checks_the_installation_again(
    fake_cli: Path, tmp_path: Path
) -> None:
    calls, mode = tmp_path / "calls", tmp_path / "mode"
    mode.write_text("crash")
    p = fake_provider(fake_cli, {"FAKE_CLI_CALLS": str(calls), "FAKE_CLI_MODE_FILE": str(mode)})
    await p.health()
    with pytest.raises(ProviderError):
        await p.complete(make_request("Hallo"))
    before = len(calls_of(calls))

    failed = await p.health()
    mode.write_text("ok")
    await p.complete(make_request("Hallo"))
    recovered = await p.health()

    assert failed.status is HealthStatus.LIMITED and "last request failed" in failed.reason
    assert calls_of(calls)[before : before + 2] == ["--version", "auth status"]
    assert recovered.status is HealthStatus.AVAILABLE


NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


async def test_a_plan_limit_keeps_claude_out_until_the_reset_and_costs_no_process(
    fake_cli: Path, tmp_path: Path
) -> None:
    calls, mode, clock = tmp_path / "calls", tmp_path / "mode", Clock()
    reset = NOW + timedelta(hours=2)
    mode.write_text("limit")
    env = {
        "FAKE_CLI_CALLS": str(calls),
        "FAKE_CLI_MODE_FILE": str(mode),
        "FAKE_CLI_RESET": str(reset.timestamp()),
    }
    p = fake_provider(fake_cli, env, now=clock)

    with pytest.raises(ProviderLimitedError) as caught:
        await p.complete(make_request("Hallo"))
    limited = await p.health()
    spawned = len(calls_of(calls))
    with pytest.raises(ProviderLimitedError):
        await p.complete(make_request("Hallo"))

    assert caught.value.limit.until == reset and caught.value.limit.kind == "five_hour"
    assert limited.status is HealthStatus.UNAVAILABLE and limited.limited_until == reset
    assert "five_hour" in limited.reason
    assert len(calls_of(calls)) == spawned  # the second request started no process

    mode.write_text("ok")
    clock.now = reset + timedelta(seconds=1)
    after = await p.complete(make_request("Hallo"))
    info = await p.health()

    assert after.text == "Echo: Hallo"
    assert info.status is not HealthStatus.UNAVAILABLE and info.limited_until is None


async def test_a_limit_told_only_in_words_takes_its_reset_time_from_the_sentence(
    fake_cli: Path,
) -> None:
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "limit_text"}, now=Clock())

    with pytest.raises(ProviderLimitedError) as caught:
        await p.complete(make_request("Hallo"))

    # "resets 11pm (Europe/Berlin)" asked at 12:00 UTC (13:00 in Berlin): tonight, 22:00 UTC.
    assert caught.value.limit.estimated is False
    assert caught.value.limit.until == datetime(2026, 3, 10, 22, 0, tzinfo=UTC)
    assert "You've hit your limit" in str(caught.value)


async def test_the_clis_own_limit_sentence_is_never_delivered_as_an_answer(
    fake_cli: Path,
) -> None:
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "limit_text"}, now=Clock())
    received: list[str] = []

    with pytest.raises(ProviderLimitedError):
        async for chunk in p.stream(make_request("Hallo")):
            received.append(chunk.delta)

    assert received == []


async def test_a_limit_in_the_middle_of_an_answer_ends_in_an_error_not_in_silence(
    fake_cli: Path,
) -> None:
    reset = (NOW + timedelta(hours=1)).timestamp()
    p = fake_provider(
        fake_cli, {"FAKE_CLI_MODE": "limit_late", "FAKE_CLI_RESET": str(reset)}, now=Clock()
    )
    received: list[str] = []

    with pytest.raises(ProviderLimitedError):
        async for chunk in p.stream(make_request("Hallo", timeout_s=20)):
            received.append(chunk.delta)

    assert received == ["Echo: "]
    assert p.active_limit() is not None


async def test_a_login_error_is_not_mistaken_for_a_limit(fake_cli: Path) -> None:
    p = fake_provider(fake_cli, {"FAKE_CLI_MODE": "logged_out"})

    with pytest.raises(ProviderUnavailableError) as caught:
        await p.complete(make_request())

    assert not isinstance(caught.value, ProviderLimitedError)
    assert p.active_limit() is None


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
