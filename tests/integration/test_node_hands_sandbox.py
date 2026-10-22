"""C5 on the real sandboxes: a node's `shell.run` cannot see the node's own folder, and a stopped
call takes its sandboxed process tree with it.

Linux: bwrap (CI sets `NOX_REQUIRE_BWRAP=1`, so a missing bwrap fails there instead of skipping).
Windows: an AppContainer, only on the CI runner (it creates a container profile and ACL entries -
nothing a developer's machine should get from a test run).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from nox.node.hands import NodeHands, ShellArgs, hand_tools
from nox.node.home import HOME_ENV, NodeHome
from nox.sandbox import Sandbox, SandboxUnavailable
from nox.sandbox.netguard import NetGuard

SECRET = "node-key-material-for-the-test"


@pytest.fixture
def sandbox() -> Sandbox:
    if sys.platform.startswith("linux"):
        from nox.sandbox.linux import LinuxSandbox

        try:
            return Sandbox(LinuxSandbox(), guard=NetGuard())
        except SandboxUnavailable as exc:
            if os.environ.get("NOX_REQUIRE_BWRAP") == "1":
                pytest.fail(f"bwrap must work in this job: {exc}")
            pytest.skip(f"no working bwrap here: {exc}")
    if sys.platform == "win32" and os.environ.get("GITHUB_ACTIONS") == "true":
        from nox.sandbox.windows import WindowsSandbox, appcontainer_available

        if not appcontainer_available():
            pytest.skip("no AppContainer support on this runner")
        # File confinement is what is tested here; the loopback probe is another test's subject.
        return Sandbox(WindowsSandbox(prefer_appcontainer=True), guard=NetGuard(), accept_weak=True)
    pytest.skip("the real sandbox runs on Linux, and on the Windows CI runner")


@pytest.fixture
def node_home(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> Iterator[NodeHome]:
    """A node folder where a real one lives - in the user's own profile, not in /tmp (which the
    Linux sandbox replaces anyway, so a test there would prove nothing)."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    folder = base / "nox-c5-test" / uuid.uuid4().hex[:8]
    monkeypatch.setenv(HOME_ENV, str(folder))
    home = NodeHome(folder)
    home.ensure()  # owner-only, as for a real node
    home.key_file.write_text(SECRET, encoding="utf-8")
    try:
        yield home
    finally:
        import shutil

        shutil.rmtree(folder.parent, ignore_errors=True)


def _tools(home: NodeHome, sandbox: Sandbox) -> dict[str, object]:
    hands = NodeHands(folders=lambda: [], protected=(home.path,), sandbox=lambda: sandbox)
    return hand_tools(hands)  # type: ignore[return-value]


async def test_the_node_folder_is_invisible_inside_the_sandbox(
    node_home: NodeHome, sandbox: Sandbox
) -> None:
    tools = _tools(node_home, sandbox)
    run = tools["shell.run"].handler  # type: ignore[attr-defined]
    key = str(node_home.key_file)
    if sys.platform == "win32":
        reads = [ShellArgs(command=f"Get-Content -LiteralPath '{key}'")]
        lists = ShellArgs(command=f"Get-ChildItem -LiteralPath '{node_home.path}'")
    else:
        reads = [ShellArgs(argv=["cat", key]), ShellArgs(command=f"cat '{key}'")]
        lists = ShellArgs(argv=["ls", "-la", str(node_home.path)])
    for args in reads:
        result = await run(args)
        assert SECRET not in result["stdout"], result
        assert not result["ok"], result
    listed = await run(lists)
    assert "node.key" not in listed["stdout"], listed
    assert not listed["ok"], listed


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux cancellation path")
async def test_a_stopped_call_takes_its_process_tree_with_it(
    node_home: NodeHome, sandbox: Sandbox
) -> None:
    tools = _tools(node_home, sandbox)
    run = tools["shell.run"].handler  # type: ignore[attr-defined]
    me = psutil.Process()
    task = asyncio.create_task(run(ShellArgs(argv=["sleep", "60"])))
    deadline = time.monotonic() + 10
    while not _sleeping(me):
        assert time.monotonic() < deadline, "the sandboxed run never started"
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline = time.monotonic() + 3
    while _sleeping(me):
        assert time.monotonic() < deadline, "the sandboxed sleep outlived its call"
        await asyncio.sleep(0.05)


def _sleeping(process: psutil.Process) -> bool:
    """Whether a `sleep 60` still runs below this test process (inside bwrap's tree)."""
    for child in process.children(recursive=True):
        try:
            if child.cmdline()[-2:] == ["sleep", "60"] or child.cmdline()[-1:] == ["60"]:
                return True
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue
    return False
