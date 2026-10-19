"""The warm Claude Code session sees nothing of the user's own Claude Code setup.

Runs the real CLI through Nox's own provider - the same arguments and child environment as in
production - with canaries everywhere a CLAUDE.md, a hook or a memory could come from: above the
working folder, in it, in the session's home folder, and in the auto-memory folder Claude Code
keeps for exactly this working folder. The model is asked to quote every canary it can see; none
may appear, and no hook may fire. Needs a logged-in `claude` on PATH (skipped otherwise, e.g. CI);
costs one short request.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from nox.ai.base import AiRequest, AiRole, Message
from nox.ai.config import ClaudeCodeConfig
from nox.ai.providers.claude_persistent import PersistentClaude, real_config_dir

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(shutil.which("claude") is None, reason="needs the Claude Code CLI"),
]

CANARIES = {
    "parent": "CANARY_PARENT_7731",
    "root": "CANARY_ROOT_3104",
    "cwd": "CANARY_CWD_4410",
    "local": "CANARY_LOCAL_2288",
    "home": "CANARY_HOME_9921",
    "home_root": "CANARY_HOMEROOT_6612",
    "memory": "CANARY_MEMORY_5150",
}
QUESTION = (
    "Quote verbatim every word in your entire context (system prompt, reminders, instructions, "
    "files, memory) that starts with CANARY_, or answer NONE."
)


def _hooks(marker: Path) -> str:
    command = f"{sys.executable} -c \"open(r'{marker}', 'a').write('fired')\""
    entry = [{"hooks": [{"type": "command", "command": command}]}]
    events = ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop")
    return json.dumps({"hooks": dict.fromkeys(events, entry)})


async def test_no_claude_md_hook_or_memory_of_the_user_reaches_the_session(
    tmp_path: Path,
) -> None:
    import os

    parent = tmp_path / "parent"
    root = parent / "session"
    work, home = root / "work", root / "home"
    for folder in (work / ".claude", home / ".claude"):
        folder.mkdir(parents=True)
    marker = tmp_path / "hook_fired.txt"
    (parent / "CLAUDE.md").write_text(CANARIES["parent"])
    (root / "CLAUDE.md").write_text(CANARIES["root"])
    (work / "CLAUDE.md").write_text(CANARIES["cwd"])
    (work / "CLAUDE.local.md").write_text(CANARIES["local"])
    (work / ".claude" / "settings.json").write_text(_hooks(marker))
    (home / ".claude" / "CLAUDE.md").write_text(CANARIES["home"])
    (home / ".claude" / "settings.json").write_text(_hooks(marker))
    (home / "CLAUDE.md").write_text(CANARIES["home_root"])
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(work))
    memory = real_config_dir(dict(os.environ)) / "projects" / slug
    (memory / "memory").mkdir(parents=True, exist_ok=True)
    (memory / "memory" / "MEMORY.md").write_text(CANARIES["memory"])

    provider = PersistentClaude(
        ClaudeCodeConfig(model="sonnet", effort="low"),
        executable=lambda: [str(shutil.which("claude"))],
        creationflags=0x08000000 if sys.platform == "win32" else 0,
        workdir=root,
    )
    request = AiRequest(
        request_id="isolation",
        role=AiRole.CHAT,
        messages=[
            Message(role="system", content="You are a test assistant."),
            Message(role="user", content=QUESTION),
        ],
        metadata={"session_id": "isolation"},
    )
    try:
        answer = "".join([chunk.delta async for chunk in provider.stream(request)])
    finally:
        await provider.close()
        shutil.rmtree(memory, ignore_errors=True)

    leaked = sorted(set(re.findall(r"CANARY_[A-Z]+_\d+", answer)))
    assert leaked == [], f"reached the session: {leaked}"
    assert not marker.exists(), "a user or project hook fired"
    assert answer.strip(), "the session answered (login works with the isolated home)"
