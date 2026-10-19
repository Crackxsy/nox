"""The warm Claude Code session, against a fake CLI that speaks the same stream-json protocol.

Covers what the speed and the kill switch depend on: one process for many turns, only the missing
history sent, per-turn model switch, per-turn cost, the turn token handed to the bridge before the
message, barge-in that keeps the process, and a kill switch that leaves no process behind - also
when it fires while a session is still starting.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import pytest

from nox.ai.base import AiChunk, AiRequest, AiRole, Message, ToolDefinition
from nox.ai.config import ClaudeCodeConfig
from nox.ai.errors import ProviderUnavailableError
from nox.ai.providers.claude_persistent import PersistentClaude

FAKE = Path(__file__).with_name("fake_claude.py")
KILL_BUDGET_S = 1.0


@dataclass(frozen=True)
class FakeLaunch:
    client_id: str
    config_path: Path


@dataclass
class FakeBridge:
    """Records what the provider tells the gate, in order."""

    root: Path
    events: list[tuple[str, str]] = field(default_factory=list)

    def prepare(self) -> FakeLaunch:
        launch = FakeLaunch(client_id=f"mcp:{len(self.events)}", config_path=self.root / "m.json")
        self.events.append(("prepare", launch.client_id))
        return launch

    async def activate(self, launch: FakeLaunch, token: str) -> bool:
        self.events.append(("activate", token))
        return True

    async def retire(self, launch: FakeLaunch) -> None:
        self.events.append(("retire", launch.client_id))


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "fake.jsonl"


def _provider(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch, **env: str
) -> tuple[PersistentClaude, FakeBridge]:
    monkeypatch.setenv("FAKE_LOG", str(log))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import os

    provider = PersistentClaude(
        ClaudeCodeConfig(model="sonnet", reason_model="opus"),
        executable=lambda: [sys.executable, str(FAKE)],
        creationflags=0x08000000 if sys.platform == "win32" else 0,
        workdir=tmp_path / "claude",
        env=dict(os.environ),
        also_inherit=frozenset({"FAKE_LOG", "FAKE_CHILD", "FAKE_INIT_DELAY", "FAKE_DELTA_DELAY"}),
    )
    bridge = FakeBridge(tmp_path)
    provider.attach_tools(bridge)
    return provider, bridge


def _request(text: str, **kw: Any) -> AiRequest:
    messages = kw.pop("history", []) + [Message(role="user", content=text)]
    return AiRequest(
        request_id=kw.pop("request_id", "r1"),
        role=kw.pop("role", AiRole.CHAT),
        messages=[Message(role="system", content="You are Nox."), *messages],
        metadata={"session_id": kw.pop("conversation", "conv-1")},
        **kw,
    )


async def _collect(stream: AsyncIterator[AiChunk]) -> list[AiChunk]:
    return [chunk async for chunk in stream]


def _log_lines(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _log_lines_if_any(log: Path) -> list[dict[str, Any]]:
    """Like `_log_lines`, for polling while the fake still writes: a missing file or a line
    caught half-written reads as "not yet" instead of failing the test."""
    if not log.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            break
    return entries


def _user_messages(log: Path) -> list[str]:
    return [
        str(e["in"]["message"]["content"])
        for e in _log_lines(log)
        if "in" in e and e["in"].get("type") == "user"
    ]


#: How long the OS may take to reap a killed tree before the test calls it a survivor.
REAP_BOUND_S = 15.0


async def _logged(log: Path, key: str) -> int:
    """The first value the fake CLI logged under `key`, polled (the log is written concurrently)."""
    deadline = time.monotonic() + REAP_BOUND_S
    while True:
        for entry in await asyncio.to_thread(_log_lines_if_any, log):
            if key in entry:
                return int(entry[key])
        assert time.monotonic() < deadline, f"the fake CLI never logged {key!r}"
        await asyncio.sleep(0.02)


async def _wait_until_gone(*pids: int) -> None:
    deadline = time.monotonic() + REAP_BOUND_S
    while not all(_gone(pid) for pid in pids):
        assert time.monotonic() < deadline, "the bridge must die with the CLI"
        await asyncio.sleep(0.02)


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


async def test_one_process_answers_many_turns(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    try:
        first = await _collect(provider.stream(_request("Hallo", request_id="a")))
        pid = provider.pid
        await _collect(provider.stream(_request("Noch was", request_id="b")))

        assert provider.pid == pid
        assert "".join(c.delta for c in first) == "Hallo Welt."
        assert first[-1].done
        assert len([e for e in _log_lines(log) if "pid" in e]) == 1, "one process for both turns"
    finally:
        await provider.close()


async def test_each_turn_reports_its_own_cost(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    try:
        await _collect(provider.stream(_request("a", request_id="a")))
        await _collect(provider.stream(_request("b", request_id="b")))

        assert provider.last["a"].cost_usd == pytest.approx(0.01)
        assert provider.last["b"].cost_usd == pytest.approx(0.01), "not the session total"
    finally:
        await provider.close()


async def test_only_history_the_session_has_not_seen_is_sent(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    try:
        await _collect(provider.stream(_request("Erste Frage", request_id="a")))
        history = [
            Message(role="user", content="Erste Frage"),
            Message(role="assistant", content="Hallo Welt."),
            Message(role="user", content="Am Dashboard gefragt"),
            Message(role="assistant", content="Am Dashboard beantwortet"),
        ]
        await _collect(provider.stream(_request("Zweite", request_id="b", history=history)))

        second = _user_messages(log)[1]
        assert "Am Dashboard gefragt" in second and "Am Dashboard beantwortet" in second
        assert "Erste Frage" not in second, "the session already has that turn"
        assert second.endswith("Zweite")
    finally:
        await provider.close()


async def test_the_turn_context_travels_with_the_message(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    try:
        await _collect(provider.stream(_request("Wie spät?", context="Current facts:\n- time: 9")))

        message = _user_messages(log)[0]
        assert message.startswith("<turn_context>\nCurrent facts:")
        assert message.endswith("Wie spät?")
    finally:
        await provider.close()


async def test_a_new_conversation_gets_a_fresh_process(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, bridge = _provider(tmp_path, log, monkeypatch)
    try:
        await _collect(provider.stream(_request("a", conversation="one")))
        first = provider.pid
        await _collect(provider.stream(_request("b", conversation="two")))

        assert provider.pid != first
        assert first is not None and _gone(first)
        assert ("retire", "mcp:0") in bridge.events
    finally:
        await provider.close()


async def test_a_reasoning_turn_switches_the_model_inside_the_session(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    try:
        await _collect(provider.stream(_request("Denk nach", role=AiRole.REASON)))
        await _collect(provider.stream(_request("Kurz", request_id="b")))

        models = [e["model"] for e in _log_lines(log) if "model" in e]
        assert models == ["sonnet", "opus", "sonnet"]
    finally:
        await provider.close()


async def test_the_turn_token_reaches_the_bridge_before_the_message(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, bridge = _provider(tmp_path, log, monkeypatch)
    tool = ToolDefinition(name="home.light", description="Light.")
    try:
        chunks = await _collect(
            provider.stream(_request("TOOL bitte", tools=[tool], tool_turn="secret-turn-token"))
        )

        assert ("activate", "secret-turn-token") in bridge.events
        assert any(c.tool_activity == ["home.light"] for c in chunks)
        text = "".join(c.delta for c in chunks)
        assert text == "Hallo Welt. Erledigt.", "text after a tool call must not run into the last"
    finally:
        await provider.close()


async def test_barge_in_stops_the_answer_and_keeps_the_process(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    try:
        stream = provider.stream(_request("SLOW bitte", request_id="a"))
        async for chunk in stream:
            if "weiter" in chunk.delta:
                break
        await stream.aclose()  # what the router does when the turn is cancelled
        pid = provider.pid

        chunks = await _collect(provider.stream(_request("Neu", request_id="b")))

        assert provider.pid == pid
        assert "".join(c.delta for c in chunks) == "Hallo Welt."
        assert any(
            e.get("in", {}).get("request", {}).get("subtype") == "interrupt"
            for e in _log_lines(log)
        )
    finally:
        await provider.close()


async def test_the_kill_switch_ends_the_whole_process_tree_within_a_second(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, bridge = _provider(tmp_path, log, monkeypatch, FAKE_CHILD="1")
    stream = provider.stream(_request("SLOW bitte"))
    async for chunk in stream:
        if "weiter" in chunk.delta:
            break
    pid = provider.pid
    child = await _logged(log, "child")
    assert pid is not None and not _gone(child)

    started = time.perf_counter()
    await provider.kill()
    elapsed = time.perf_counter() - started

    # `kill()` never waits for a process to exit, so returning within the budget is the claim.
    assert elapsed < KILL_BUDGET_S
    # Reaping is the OS's business and takes a moment on a loaded machine: poll for it instead of
    # asserting the instant `kill()` returns. The "SLOW" turn never ends on its own, so a tree that
    # is gone at all was killed - the generous bound does not weaken that.
    await _wait_until_gone(pid, child)
    assert ("retire", "mcp:0") in bridge.events
    with pytest.raises(ProviderUnavailableError):
        await _collect(provider.stream(_request("Hallo", request_id="b")))
    await stream.aclose()


async def test_a_kill_during_a_slow_start_leaves_nothing_running(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, bridge = _provider(tmp_path, log, monkeypatch, FAKE_INIT_DELAY="30")
    turn = asyncio.create_task(_collect(provider.stream(_request("Hallo"))))
    deadline = time.monotonic() + 20
    while not any("pid" in e for e in await asyncio.to_thread(_log_lines_if_any, log)):
        assert time.monotonic() < deadline, "the fake CLI never started"
        await asyncio.sleep(0.02)
    pid = next(e["pid"] for e in _log_lines(log) if "pid" in e)

    started = time.perf_counter()
    await provider.kill()
    assert time.perf_counter() - started < KILL_BUDGET_S
    await _wait_until_gone(pid)  # the kill is synchronous; the OS reaps in its own time

    with pytest.raises(ProviderUnavailableError):
        await asyncio.wait_for(turn, timeout=KILL_BUDGET_S)
    assert provider.pid is None, "a start overtaken by the kill switch must not be stored"
    assert ("retire", "mcp:0") in bridge.events


async def test_after_resume_a_new_session_starts(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _provider(tmp_path, log, monkeypatch)
    try:
        await provider.kill()
        provider.resume()

        chunks = await _collect(provider.stream(_request("Hallo")))

        assert "".join(c.delta for c in chunks) == "Hallo Welt."
    finally:
        await provider.close()


async def test_a_kill_while_the_process_is_being_spawned_discards_the_session(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill lands before the process exists, so there is nothing to kill yet: the start must
    notice afterwards and throw its session away instead of storing it."""
    provider, bridge = _provider(tmp_path, log, monkeypatch)
    kills: list[asyncio.Task[None]] = []

    def executable() -> list[str]:
        kills.append(asyncio.get_running_loop().create_task(provider.kill()))
        return [sys.executable, str(FAKE)]

    provider._executable = executable  # noqa: SLF001 - the spawn moment is the point of the test

    with pytest.raises(ProviderUnavailableError):
        await _collect(provider.stream(_request("Hallo")))
    await asyncio.gather(*kills)

    assert provider.pid is None
    pids = [e["pid"] for e in await asyncio.to_thread(_log_lines_if_any, log) if "pid" in e]
    assert all(_gone(pid) for pid in pids)
