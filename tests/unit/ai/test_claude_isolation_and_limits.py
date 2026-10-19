"""The warm Claude Code session: isolation from the user's setup, a clean child environment, a
fresh process when the offer changes or the bridge is lost, giving the session up after repeated
failures (to one-shot Claude, never silently to another provider), cost and web-search limits,
private history, halting on a privacy change, and a kill switch that never waits.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import psutil
import pytest

from nox.ai.base import Message, ToolDefinition
from nox.ai.config import ClaudeCodeConfig
from nox.ai.errors import ProviderError, ProviderUnavailableError
from nox.ai.providers import claude_session
from nox.ai.providers.claude_code import ClaudeCodeProvider
from nox.ai.providers.claude_persistent import (
    MAX_SESSION_FAILURES,
    MAX_WEB_SEARCHES_PER_TURN,
    PersistentClaude,
    child_env,
)
from nox.core.events import HealthStatus

from .test_claude_persistent import (
    FAKE,
    _collect,
    _log_lines,
    _provider,
    _request,
    _user_messages,
)
from .test_claude_persistent import log as log  # noqa: F401 - the shared fixture

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _interrupted(log: Path) -> bool:
    return any(
        e.get("in", {}).get("request", {}).get("subtype") == "interrupt" for e in _log_lines(log)
    )


def test_the_child_environment_is_an_allow_list() -> None:
    base = {
        "PATH": "p",
        "SYSTEMROOT": "s",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_SSE_PORT": "1234",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        "CLAUDE_CODE_GIT_BASH_PATH": "bash",
        "NOX_WORKER_TOKEN": "nox-secret",
        "TWITCH_CLIENT_SECRET": "secret",
        "USERPROFILE": str(Path("users") / "someone"),
    }

    env = child_env(base, home=Path("isolated-home"))

    assert env["PATH"] == "p" and env["SYSTEMROOT"] == "s"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
    assert env["CLAUDE_CODE_GIT_BASH_PATH"] == "bash"
    for dropped in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        assert dropped not in env
    assert "NOX_WORKER_TOKEN" not in env and "TWITCH_CLIENT_SECRET" not in env
    assert env["HOME"] == env["USERPROFILE"] == "isolated-home"
    assert Path(env["CLAUDE_CONFIG_DIR"]) == Path("users") / "someone" / ".claude"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_the_session_runs_isolated_from_user_settings_and_memory(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)

    spec = provider.spec("You are Nox.", "balanced", None)

    args = list(spec.args)
    assert args[args.index("--setting-sources") + 1] == ""
    assert json.loads(args[args.index("--settings") + 1]) == {"autoMemoryEnabled": False}
    assert "--disable-slash-commands" in args and "--safe-mode" not in args
    assert spec.cwd == tmp_path / "claude" / "work"
    assert spec.env is not None and spec.env["HOME"] == str(tmp_path / "claude" / "home")


def test_without_a_given_folder_the_session_lives_in_the_system_temp_folder() -> None:
    provider = PersistentClaude(
        ClaudeCodeConfig(), executable=list, creationflags=0, workdir=None, env={}
    )

    spec = provider.spec("", "balanced", None)

    assert spec.cwd is not None
    assert spec.cwd.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())


async def test_a_different_offer_starts_a_fresh_process(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code lists the tools once, at start: a new offer needs a new process."""
    provider, _ = _provider(tmp_path, log, monkeypatch)
    light = ToolDefinition(name="home.light", description="Light.")
    files = ToolDefinition(name="file.read", description="Read.")
    try:
        await _collect(provider.stream(_request("a", request_id="a", tools=[light])))
        first = provider.pid
        await _collect(provider.stream(_request("b", request_id="b", tools=[light])))
        same = provider.pid
        await _collect(provider.stream(_request("c", request_id="c", tools=[light, files])))

        assert same == first
        assert provider.pid != first
    finally:
        await provider.close()


async def test_a_lost_bridge_replaces_the_session_before_the_next_turn(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    try:
        await _collect(provider.stream(_request("a", request_id="a")))
        first = provider.pid
        provider.bridge_lost("mcp:another-client")
        await _collect(provider.stream(_request("b", request_id="b")))
        assert provider.pid == first, "a foreign disconnect changes nothing"

        provider.bridge_lost("mcp:0")
        await _collect(provider.stream(_request("c", request_id="c")))

        assert provider.pid != first
    finally:
        await provider.close()


async def test_repeated_start_failures_turn_the_session_off(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch, FAKE_FAIL_INIT="1")
    provider._also_inherit = frozenset({"FAKE_LOG", "FAKE_FAIL_INIT"})  # noqa: SLF001
    for index in range(MAX_SESSION_FAILURES):
        with pytest.raises(ProviderError):
            await _collect(provider.stream(_request("Hallo", request_id=f"r{index}")))

    assert provider.disabled and "start" in provider.disabled_reason
    assert "unknown control request" in provider.last_failure, "the CLI's stderr is kept"
    with pytest.raises(ProviderUnavailableError):
        await _collect(provider.stream(_request("Hallo", request_id="after")))


async def test_the_provider_falls_back_to_one_shot_claude_never_silently_elsewhere(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_FAIL_INIT", "1")
    provider = ClaudeCodeProvider(
        ClaudeCodeConfig(health_roundtrip=False),
        command_override=[sys.executable, str(FAKE)],
        workdir=tmp_path / "claude",
        env=dict(os.environ),
    )
    provider.conversation._also_inherit = frozenset({"FAKE_LOG", "FAKE_FAIL_INIT"})  # noqa: SLF001

    texts = []
    for index in range(MAX_SESSION_FAILURES + 1):
        chunks = await _collect(provider.stream(_request("Hallo", request_id=f"r{index}")))
        texts.append("".join(c.delta for c in chunks))

    assert texts == ["Einmal."] * (MAX_SESSION_FAILURES + 1), "every turn answered by Claude"
    assert provider.conversation.disabled
    info = await provider.health()
    assert info.status is HealthStatus.LIMITED
    assert "conversation session off" in info.reason


async def test_the_session_cost_cap_is_passed_and_web_searches_are_bounded(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOG", str(log))
    provider = PersistentClaude(
        ClaudeCodeConfig(max_budget_usd=0.05, session_turns=10),
        executable=lambda: [sys.executable, str(FAKE)],
        creationflags=NO_WINDOW,
        workdir=tmp_path / "claude",
        env=dict(os.environ),
        also_inherit=frozenset({"FAKE_LOG"}),
    )
    args = list(provider.spec("", "balanced", None).args)
    assert args[args.index("--max-budget-usd") + 1] == "0.5000"
    try:
        chunks = await _collect(provider.stream(_request("SEARCH bitte")))

        searches = [c for c in chunks if c.tool_activity == ["WebSearch"]]
        assert len(searches) <= MAX_WEB_SEARCHES_PER_TURN + 1
        assert _interrupted(log), "the turn is interrupted once the search budget is spent"
    finally:
        await provider.close()


async def test_private_history_never_reaches_the_session(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    history = [
        Message(role="user", content="Offline gefragt", private=True),
        Message(role="assistant", content="Offline beantwortet", private=True),
        Message(role="user", content="Normal gefragt"),
        Message(role="assistant", content="Normal beantwortet"),
    ]
    try:
        await _collect(provider.stream(_request("Jetzt", history=history)))

        message = _user_messages(log)[0]
        assert "Offline" not in message
        assert "Normal gefragt" in message
    finally:
        await provider.close()


def test_the_one_shot_prompt_leaves_private_history_out_and_carries_the_context() -> None:
    from nox.ai.providers.claude_code import render_prompt

    request = _request(
        "Jetzt",
        context="Current facts:\n- time: 9",
        history=[
            Message(role="user", content="Offline gefragt", private=True),
            Message(role="user", content="Normal gefragt"),
        ],
    )

    system, prompt = render_prompt(request)

    assert system.endswith("- time: 9")
    assert "Offline" not in prompt and "Normal gefragt" in prompt


async def test_halt_stops_the_answer_in_progress(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    try:
        stream = provider.stream(_request("SLOW bitte"))
        async for chunk in stream:
            if "weiter" in chunk.delta:
                provider.halt()
                break
        await stream.aclose()

        chunks = await _collect(provider.stream(_request("Weiter", request_id="b")))

        assert "".join(c.delta for c in chunks) == "Hallo Welt."
        assert _interrupted(log)
    finally:
        await provider.close()


async def test_a_kill_while_the_old_session_closes_starts_nothing_new(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch, FAKE_SLOW_EXIT="1.5")
    provider._also_inherit = frozenset({"FAKE_LOG", "FAKE_SLOW_EXIT"})  # noqa: SLF001
    await _collect(provider.stream(_request("a", conversation="one")))
    switch = asyncio.create_task(_collect(provider.stream(_request("b", conversation="two"))))
    deadline = asyncio.get_running_loop().time() + 10
    while provider._session is not None:  # noqa: SLF001 - the old session is being closed now
        assert asyncio.get_running_loop().time() < deadline, "the old session was never closed"
        await asyncio.sleep(0.01)

    await provider.kill()

    with pytest.raises(ProviderUnavailableError):
        await switch
    assert len([e for e in _log_lines(log) if "pid" in e]) == 1, "no second process was started"


def test_the_kill_never_waits_on_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("wait_procs blocks the event loop")

    monkeypatch.setattr(psutil, "wait_procs", forbidden)
    child = psutil.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    claude_session.kill_tree(child.pid)

    assert child.wait(timeout=10) is not None


async def test_a_turn_is_stopped_honestly_when_its_estimated_cost_passes_the_limit(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--max-budget-usd counts the whole session; the per-turn limit is enforced while the turn
    runs, from the usage Claude Code streams."""
    monkeypatch.setenv("FAKE_LOG", str(log))
    provider = PersistentClaude(
        ClaudeCodeConfig(model="sonnet", max_budget_usd=0.25, session_turns=40),
        executable=lambda: [sys.executable, str(FAKE)],
        creationflags=NO_WINDOW,
        workdir=tmp_path / "claude",
        env=dict(os.environ),
        also_inherit=frozenset({"FAKE_LOG"}),
    )
    try:
        chunks = await _collect(provider.stream(_request("EXPENSIVE bitte")))

        text = "".join(c.delta for c in chunks)
        assert "Kostenlimit" in text
        assert text.count("Schritt.") <= 4, "stopped after about 0.25 USD of sonnet steps"
        assert _interrupted(log)
        assert chunks[-1].done
    finally:
        await provider.close()


def test_the_cost_estimate_prices_unknown_models_like_the_most_expensive_family() -> None:
    from nox.ai.providers.claude_cost import TurnCost

    known, unknown = TurnCost(model="claude-sonnet-5-5"), TurnCost(model="mystery")
    for cost in (known, unknown):
        cost.step_started(input_tokens=1_000_000, cache_write_tokens=0, cache_read_tokens=0)
        cost.output_so_far(100_000)

    assert known.usd == pytest.approx(2.0 + 1.0)
    assert unknown.usd == pytest.approx(4.0 + 2.0)
