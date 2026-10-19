"""The MCP runtime's reaction to a privacy change: open turns end, the answer is halted."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nox.ai.tooluse import OfferedTool
from nox.core.events import E, Event
from nox.core.toolloop import ToolLoop
from nox.mcp.install import McpRuntime


@dataclass
class FakeConversation:
    halted: int = 0

    def halt(self) -> None:
        self.halted += 1


@dataclass
class FakeProvider:
    conversation: FakeConversation = field(default_factory=FakeConversation)


async def _never(_name: str, _arguments: dict[str, Any]) -> Any:
    raise AssertionError("no tool may run")


def _runtime(cloud: list[bool]) -> tuple[McpRuntime, ToolLoop, FakeProvider]:
    loop = ToolLoop(offer=lambda: [OfferedTool(name="home.light", description="x")], call=_never)
    provider = FakeProvider()
    runtime = McpRuntime(
        gate=None,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        cloud_allowed=lambda: cloud[0],
        tool_loop=lambda: loop,
    )
    return runtime, loop, provider


async def test_private_mode_ends_open_turns_and_halts_the_session() -> None:
    cloud = [True]
    runtime, loop, provider = _runtime(cloud)
    turn = loop.begin("t")

    cloud[0] = False
    await runtime._on_privacy(Event(name=E.PRIVACY_MODE_CHANGED, payload={}))  # noqa: SLF001

    assert turn.closed and loop.by_token(turn.token) is None
    assert provider.conversation.halted == 1


async def test_a_change_that_still_allows_the_cloud_changes_nothing() -> None:
    runtime, loop, provider = _runtime([True])
    turn = loop.begin("t")

    await runtime._on_privacy(Event(name=E.PRIVACY_MODE_CHANGED, payload={}))  # noqa: SLF001

    assert not turn.closed and provider.conversation.halted == 0
