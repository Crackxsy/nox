"""Isolation every integration test gets: a real `NoxCore` never reaches into this machine's own
Documents folder.

The Rocket League extension backfills every replay under
`Documents/My Games/Rocket League/TAGame/DemosEpic`. On a developer's machine that is thousands of
real replays, parsed in the background of every test that boots a core - slow, a source of
timeouts under load, and the developer's own data. CI has none, so it never noticed. Each test
gets an empty Documents folder of its own instead; a test that wants replays puts them there or
passes its own folder, as `test_rl_plugin.py` does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _empty_documents_folder(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    documents = tmp_path_factory.mktemp("Documents")
    monkeypatch.setattr("nox.rl.paths.documents_dir", lambda: documents)
    return documents


#: Where a test core's default AI providers point instead of this machine's: the discard port
#: (nothing listens, the connection is refused at once) and a command that does not exist.
UNREACHABLE_OLLAMA = "http://127.0.0.1:9"
NO_CLAUDE_CLI = "nox-test-claude-cli-not-installed"


@pytest.fixture(autouse=True)
def _no_real_ai_providers(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A core booted by a test never talks to the developer's own Ollama or Claude Code CLI.

    Both are on by default and reachable on a developer's machine (CI has neither, so it never
    noticed): a chat turn then waited for a real local model - under load past the test timeout -
    or spent the developer's Claude quota. Providers configured explicitly by a test (a fake CLI,
    a fake server URL) are left alone, and so are tests marked `network`.
    """
    if request.node.get_closest_marker("network") is not None:
        return
    from nox.ai.providers.claude_code import ClaudeCodeProvider
    from nox.ai.providers.ollama import OllamaProvider

    ollama_init = OllamaProvider.__init__
    claude_init = ClaudeCodeProvider.__init__

    def ollama(self: OllamaProvider, config: Any, *args: Any, **kwargs: Any) -> None:
        if config.base_url == "http://127.0.0.1:11434":
            config = config.model_copy(update={"base_url": UNREACHABLE_OLLAMA})
        ollama_init(self, config, *args, **kwargs)

    def claude(self: ClaudeCodeProvider, config: Any, *args: Any, **kwargs: Any) -> None:
        if config.command == "claude":
            config = config.model_copy(update={"command": NO_CLAUDE_CLI})
        claude_init(self, config, *args, **kwargs)

    monkeypatch.setattr(OllamaProvider, "__init__", ollama)
    monkeypatch.setattr(ClaudeCodeProvider, "__init__", claude)


@pytest.fixture(autouse=True)
def _no_claude_prewarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A booted core would start a warm Claude Code process for its first turn; a test that wants
    one starts it itself."""
    monkeypatch.setattr("nox.mcp.install.prewarm_allowed", lambda _provider: False)
