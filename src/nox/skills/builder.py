"""Who writes a skill: a Claude Code session that can read and edit one folder and nothing else.

The arguments are the coding plugin's session shape (`plugins/coding`, `build_session_args`):
`--tools Read,Edit,Write,Glob,Grep` - deliberately no shell, so the session cannot run what it
writes - `--add-dir` the skill's own work folder and `--strict-mcp-config`. Measured on the coding
plugin: only `--tools` restricts the tool set; `--allowedTools` alone does not.

What those tools may touch is not left to the CLI's defaults: the session runs in `dontAsk` mode
(nobody is there to click, and anything not allowed is refused rather than asked) with exactly
two allow rules from `--settings`, `Read(./**)` and `Edit(./**)` - the work folder, which is the
session's working directory. Read rules also bind Glob and Grep, Edit rules also bind Write. So the
builder cannot read the user's files, Nox's home or another skill, even on a CLI that would
otherwise let a session read outside its folder. `--setting-sources ""` loads no settings file at
all - not the user's, and not a `.claude/settings.json` the builder itself wrote into its folder
in an earlier round to widen the next one.

The session is told what a skill is (a Nox plugin) and what the core will refuse; it is not trusted
to have listened. `nox.skills.checks` and the tests decide, and their findings come back as the
next round's instructions, at most `max_repair_rounds` times.

Without Claude Code - not installed, or the privacy mode forbids the cloud - there is no builder,
and `unavailable()` says so in words the user hears.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "BUILDER_SETTINGS",
    "BUILD_TOOLS",
    "BuildOutcome",
    "BuildRequest",
    "ClaudeCodeSkillBuilder",
    "SkillBuilder",
    "build_prompt",
    "neutralise_wish",
    "skill_builder_args",
]

BUILD_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")

#: The builder's whole permission set: its own folder (the working directory), nothing else.
BUILDER_SETTINGS: dict[str, Any] = {
    "permissions": {
        "defaultMode": "dontAsk",
        "allow": ["Read(./**)", "Edit(./**)"],
        "additionalDirectories": [],
    }
}

#: Anything that could open or close the `<user_wish>` block, in any spelling.
_WISH_TAG = re.compile(r"<\s*/?\s*user_wish[^>]*>?", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class BuildRequest:
    skill_id: str
    intent: str
    work_dir: Path
    #: The previous round's problems, empty on the first round.
    feedback: tuple[str, ...] = ()
    round: int = 1


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    ok: bool
    message: str = ""


class SkillBuilder(Protocol):
    def unavailable(self) -> str | None:
        """Why no skill can be built right now, or None."""

    async def build(self, request: BuildRequest) -> BuildOutcome: ...


def skill_builder_args(work_dir: Path, *, max_turns: int, model: str = "") -> list[str]:
    """Everything after the executable; the prompt goes to stdin."""
    args = [
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--settings",
        json.dumps(BUILDER_SETTINGS, separators=(",", ":")),
        "--add-dir",
        str(work_dir),
        "--tools",
        ",".join(BUILD_TOOLS),
        "--max-turns",
        str(max_turns),
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--no-session-persistence",
    ]
    if model:
        args += ["--model", model]
    return args


def neutralise_wish(intent: str) -> str:
    """The user's wish as data: no tag inside it can close (or reopen) the `<user_wish>` block."""
    return _WISH_TAG.sub("[user_wish]", intent).replace("<", "‹").replace(">", "›")


def build_prompt(request: BuildRequest) -> str:
    sid = request.skill_id
    package = f"nox_skill_{sid}"
    lines = [
        "You are writing a small capability ('skill') for Nox, a local assistant. The user asked:",
        f"<user_wish>{neutralise_wish(request.intent)}</user_wish>",
        "",
        f"Write a Nox plugin in the current folder ({request.work_dir.as_posix()}),",
        "and nowhere else:",
        "- manifest.yaml with: id: " + sid + ", name, version: 0.1.0, api_version: 1,",
        f"  entry: {package}:create, profiles: [], permissions (one entry per tool:",
        f"  tool: {sid}.<verb>, risk: medium or high), events: {{emits: [], listens: []}},",
        "  secrets: [] (or nox/" + sid + "/<key> if a credential is unavoidable),",
        "  network: {egress: [host:443, ...]} listing every public host it calls, nothing else.",
        f"- src/{package}/__init__.py with `def create(api)` that registers each tool:",
        "  api.tools.register(name, InputModel, handler, Risk.MEDIUM, description=...)",
        "  where InputModel is a pydantic BaseModel and handler is `async def h(data) -> dict`.",
        "  For HTTP use only `async with api.http() as client: await client.get(...)`.",
        "- tests/test_<something>.py with plain `def test_...()` or `async def test_...()`",
        "  functions using assert. No pytest, no fixtures, no network: test the parsing and logic",
        "  with fake data (unittest.mock is allowed). At least one test.",
        "",
        "Hard rules - the core checks them and refuses the skill otherwise:",
        "- Imports only from: the standard modules json, re, math, datetime, time, decimal,",
        "  fractions, statistics, random, string, textwrap, unicodedata, uuid, zoneinfo, calendar,",
        "  html, html.parser, urllib.parse, base64, hashlib, hmac, difflib, csv, itertools,",
        "  functools, operator, collections, dataclasses, enum, typing, abc, copy, heapq, bisect,",
        "  contextlib, asyncio; plus pydantic, `from nox.plugins.api import PluginApi` and",
        "  `from nox.security.model import Risk`, and your own package.",
        "- Never: os, sys, subprocess, ctypes, socket, pathlib, open(), eval/exec, importlib,",
        "  getattr with computed names, dunder attributes, httpx/requests directly, files, keys,",
        "  the mouse or keyboard, other programs, Home Assistant, locks, alarms, games.",
        "- Tool names must not start with security, home, desktop, game, permission or mcp.",
        "- Keep it small and readable. Do not create any other files or folders.",
    ]
    if request.feedback:
        lines += [
            "",
            f"This is repair round {request.round}. The core refused the last version because:",
            *(f"- {problem}" for problem in request.feedback[:20]),
            "Fix exactly these problems.",
        ]
    return "\n".join(lines)


class ClaudeCodeSkillBuilder:
    """Runs one Claude Code session per round; see the module docstring."""

    def __init__(
        self,
        *,
        command: str,
        allows_cloud: Callable[[], bool],
        max_turns: int,
        timeout_s: float,
        model: str = "",
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = command
        self._allows_cloud = allows_cloud
        self._max_turns = max_turns
        self._timeout_s = timeout_s
        self._model = model
        self._env = env
        self._process: asyncio.subprocess.Process | None = None

    def _executable(self) -> str | None:
        return shutil.which(self._command)

    def unavailable(self) -> str | None:
        if not self._allows_cloud():
            return (
                "learning needs Claude Code, which the current privacy mode does not allow; "
                "switch to balanced or full and ask again"
            )
        if self._executable() is None:
            return (
                "learning needs Claude Code, and it is not installed here - without it I cannot "
                "teach myself anything right now"
            )
        return None

    async def build(self, request: BuildRequest) -> BuildOutcome:
        reason = self.unavailable()
        if reason is not None:
            return BuildOutcome(False, reason)
        executable = self._executable()
        assert executable is not None
        request.work_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._process = await asyncio.create_subprocess_exec(
            executable,
            *skill_builder_args(request.work_dir, max_turns=self._max_turns, model=self._model),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(request.work_dir),
            env=self._env,
            **kwargs,
        )
        try:
            out, err = await asyncio.wait_for(
                self._process.communicate(build_prompt(request).encode("utf-8")), self._timeout_s
            )
        except TimeoutError:
            await self.cancel()
            return BuildOutcome(False, f"the build took longer than {self._timeout_s:g} s")
        finally:
            code = self._process.returncode if self._process else None
            self._process = None
        if code != 0:
            tail = (err or b"").decode("utf-8", errors="replace")[-300:]
            return BuildOutcome(False, f"Claude Code ended with code {code}: {tail}".strip())
        return BuildOutcome(True, _summary(out))

    async def cancel(self) -> None:
        """Kill switch: end a running session at once."""
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 5.0)


def _summary(out: bytes) -> str:
    try:
        data = json.loads(out.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    result = data.get("result") if isinstance(data, dict) else None
    return str(result or "")[:500]
