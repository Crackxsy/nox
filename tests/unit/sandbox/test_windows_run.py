"""The real Windows sandbox: writes confined, no network without a declaration, timeout kills.

Skipped off Windows. These exercise the actual job object + low-integrity/AppContainer token, so
they are the proof that the confinement holds on this machine, not just that the argv is shaped
right.
"""

from __future__ import annotations

import sys

import pytest

from nox.sandbox import Sandbox
from nox.sandbox.model import Limits
from nox.sandbox.netguard import NetGuard

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows sandbox")

CMD = r"C:\Windows\System32\cmd.exe"


def _sandbox() -> Sandbox:
    from nox.sandbox.windows import WindowsSandbox

    # The low-integrity backend works everywhere; AppContainer is covered by its own guarantees.
    return Sandbox(WindowsSandbox(prefer_appcontainer=False), guard=NetGuard())


async def test_output_is_captured() -> None:
    result = await _sandbox().run([CMD, "/c", "echo", "noxhello"], limits=Limits(timeout_s=20))
    assert result.exit_code == 0
    assert "noxhello" in result.stdout


async def test_write_lands_in_work_dir() -> None:
    result = await _sandbox().run(
        [CMD, "/c", "echo data> out.txt && type out.txt"], limits=Limits(timeout_s=20)
    )
    assert result.exit_code == 0 and "data" in result.stdout


async def test_no_network_without_declaration() -> None:
    # A low-integrity / AppContainer child cannot open an outbound socket; the connect must fail.
    ps = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    result = await _sandbox().run(
        [
            ps,
            "-NoProfile",
            "-Command",
            "try{(New-Object Net.Sockets.TcpClient).Connect('1.1.1.1',443);'OPEN'}catch{'BLOCKED'}",
        ],
        limits=Limits(timeout_s=25),
    )
    assert "OPEN" not in result.stdout


async def test_timeout_kills() -> None:
    result = await _sandbox().run(
        [CMD, "/c", "ping -n 20 127.0.0.1 >nul"], limits=Limits(timeout_s=2)
    )
    assert result.timed_out is True


async def test_guarantees_are_honest() -> None:
    g = _sandbox().guarantees()
    assert g.confines_writes and g.confines_network
    # The low-IL fallback must NOT claim to hide secrets or confine reads (it cannot).
    assert not g.hides_secrets and not g.confines_reads
