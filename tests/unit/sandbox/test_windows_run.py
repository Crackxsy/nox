"""The real Windows sandbox: writes confined, network honest, output drained, handles not leaked.

Skipped off Windows. These exercise the actual job object + low-integrity/AppContainer token, so
they are the proof that the confinement holds on this machine, not just that the argv is shaped
right. Nothing here opens a window (`CREATE_NO_WINDOW`, `SW_HIDE`).
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time

import pytest

from nox.sandbox import Sandbox, SandboxUnavailable
from nox.sandbox.model import EgressDecl, Limits
from nox.sandbox.netguard import NetGuard
from nox.sandbox.proxy import ProxyDecision

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows sandbox")

CMD = r"C:\Windows\System32\cmd.exe"
PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
CURL = r"C:\Windows\System32\curl.exe"
HAS_CURL = os.path.exists(CURL)


def _low_il(*, accept_weak: bool = True) -> Sandbox:
    from nox.sandbox.windows import WindowsSandbox

    return Sandbox(
        WindowsSandbox(prefer_appcontainer=False), guard=NetGuard(), accept_weak=accept_weak
    )


def _appcontainer(audit: list[ProxyDecision] | None = None) -> Sandbox:
    from nox.sandbox.windows import WindowsSandbox, appcontainer_available

    if not appcontainer_available():
        pytest.skip("AppContainer profile cannot be created here")
    # accept_weak: on a machine without enforced loopback isolation the container is reported as
    # not strong (see test_appcontainer_guarantees_match_the_measurement); these tests are about
    # the network path, not that policy.
    return Sandbox(
        WindowsSandbox(prefer_appcontainer=True),
        guard=NetGuard(),
        audit=audit.append if audit is not None else None,
        accept_weak=True,
    )


async def test_output_is_captured() -> None:
    result = await _low_il().run([CMD, "/c", "echo", "noxhello"], limits=Limits(timeout_s=20))
    assert result.exit_code == 0
    assert "noxhello" in result.stdout


async def test_write_lands_in_work_dir() -> None:
    result = await _low_il().run(
        [CMD, "/c", "echo data> out.txt && type out.txt"], limits=Limits(timeout_s=20)
    )
    assert result.exit_code == 0 and "data" in result.stdout


async def test_no_network_without_declaration() -> None:
    # The child must actually run and report that the connect failed: a positive BLOCKED marker,
    # so a PowerShell that never started cannot pass this test (review finding 3).
    result = await _appcontainer().run(
        [
            PS,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "try{(New-Object Net.Sockets.TcpClient).Connect('1.1.1.1',443);'OPEN'}catch{'BLOCKED'}",
        ],
        limits=Limits(timeout_s=40),
    )
    assert "BLOCKED" in result.stdout, (result.stdout, result.stderr)
    assert "OPEN" not in result.stdout


async def test_timeout_kills() -> None:
    result = await _low_il().run(
        [CMD, "/c", "ping -n 20 127.0.0.1 >nul"], limits=Limits(timeout_s=2)
    )
    assert result.timed_out is True


async def test_low_il_guarantees_are_honest() -> None:
    g = _low_il().guarantees()
    assert g.confines_writes
    # Integrity level restricts neither reads, nor the credential store, nor sockets.
    assert not g.hides_secrets and not g.confines_reads
    assert not g.confines_network
    assert not g.strong


async def test_low_il_is_refused_unless_the_weaker_mode_was_accepted() -> None:
    with pytest.raises(SandboxUnavailable, match="accept"):
        await _low_il(accept_weak=False).run([CMD, "/c", "echo", "x"], limits=Limits(timeout_s=20))


async def test_low_il_refuses_a_run_that_declares_network() -> None:
    # Nothing could hold a low-integrity child to the proxy, so egress is refused outright.
    with pytest.raises(SandboxUnavailable, match="network"):
        await _low_il().run(
            [CMD, "/c", "echo", "x"],
            egress=EgressDecl(hosts=("example.com",)),
            limits=Limits(timeout_s=20),
        )


async def test_appcontainer_guarantees_match_the_measurement() -> None:
    from nox.sandbox.windows import host_loopback_reachable

    sandbox = _appcontainer()
    g = sandbox.guarantees()
    sid = sandbox._backend.container_sid  # type: ignore[attr-defined]
    assert g.confines_reads and g.hides_secrets and g.confines_writes
    assert g.confines_network is (not host_loopback_reachable(sid))


async def test_large_output_does_not_hang() -> None:
    # Review finding 10: a single read after the wait deadlocked as soon as the child filled the
    # pipe (~4 KB). 1 MB on stdout and stderr each must finish quickly, capped and flagged.
    script = (
        "$s='x'*1048576; [Console]::Out.Write($s); [Console]::Error.Write($s); "
        "[Console]::Out.Write('END')"
    )
    started = time.monotonic()
    result = await _low_il().run(
        [PS, "-NoProfile", "-NonInteractive", "-Command", script],
        limits=Limits(timeout_s=60, max_output_bytes=4096),
    )
    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.truncated is True
    assert len(result.stdout) == 4096 and len(result.stderr) == 4096
    assert time.monotonic() - started < 45


async def test_unrelated_inheritable_handles_do_not_leak_into_the_child() -> None:
    # Review finding 8: bInheritHandles=TRUE without PROC_THREAD_ATTRIBUTE_HANDLE_LIST hands the
    # child every inheritable handle of the core. Make one (the write end of a pipe), run a child
    # that lives ~4 s, drop our own copy and read: with no leak EOF comes at once; with a leak the
    # read blocks until the child exits.
    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    run = asyncio.ensure_future(
        _low_il().run([CMD, "/c", "ping -n 5 127.0.0.1 >nul"], limits=Limits(timeout_s=30))
    )
    await asyncio.sleep(1.5)  # the child exists now
    os.close(write_fd)
    got_eof = threading.Event()

    def reader() -> None:
        if os.read(read_fd, 1) == b"":
            got_eof.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout=1.5)
    leaked = not got_eof.is_set()
    await run
    thread.join(timeout=10)
    os.close(read_fd)
    assert not leaked, "the sandboxed child inherited an unrelated handle of the core"


def _online() -> bool:
    try:
        socket.create_connection(("example.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


async def test_appcontainer_declared_egress_reaches_only_the_declared_host() -> None:
    # Review finding 11: the declared egress path must really work in an AppContainer (named pipe
    # to the proxy + in-container forwarder), and still only through the proxy.
    if not HAS_CURL:
        pytest.skip("curl.exe not present")
    if not _online():
        pytest.skip("no internet on this machine")
    decisions: list[ProxyDecision] = []
    sandbox = _appcontainer(decisions)
    allowed = await sandbox.run(
        [CURL, "-s", "-o", "NUL", "-m", "20", "-w", "%{http_code}", "https://example.com/"],
        egress=EgressDecl(hosts=("example.com",)),
        limits=Limits(timeout_s=60),
    )
    assert allowed.stdout.strip() == "200", (allowed.stdout, allowed.stderr)
    denied = await sandbox.run(
        [CURL, "-s", "-o", "NUL", "-m", "20", "-w", "%{http_code}", "https://www.wikipedia.org/"],
        egress=EgressDecl(hosts=("example.com",)),
        limits=Limits(timeout_s=60),
    )
    assert denied.stdout.strip() != "200"
    assert any(d.allowed and d.host == "example.com" for d in decisions)
    assert any(not d.allowed and d.host == "www.wikipedia.org" for d in decisions)


async def test_appcontainer_without_declaration_cannot_reach_the_internet() -> None:
    # No declaration: no proxy, no forwarder, and a direct HTTPS attempt fails inside.
    if not HAS_CURL:
        pytest.skip("curl.exe not present")
    result = await _appcontainer().run(
        [CURL, "-s", "-o", "NUL", "-m", "10", "-w", "%{http_code}", "https://example.com/"],
        limits=Limits(timeout_s=40),
    )
    assert result.stdout.strip() in ("000", ""), result.stdout
