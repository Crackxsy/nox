"""The real Windows sandbox: writes confined, network honest, output drained, handles not leaked.

Skipped off Windows. These exercise the actual job object + low-integrity/AppContainer token, so
they are the proof that the confinement holds on this machine, not just that the argv is shaped
right. Nothing here opens a window (`CREATE_NO_WINDOW`, `SW_HIDE`).
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
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
            "try{[System.Net.Sockets.TcpClient]::new().Connect('1.1.1.1',443);'OPEN'}"
            "catch [System.Net.Sockets.SocketException]{'BLOCKED'}catch{'ERROR'}",
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


@pytest.mark.network
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
        # `-p` tunnels plain HTTP through CONNECT too: no TLS stack (and no certificate revocation
        # lookup, which cannot leave the container either) between the test and the pipe path.
        [CURL, "-s", "-p", "-o", "NUL", "-m", "20", "-w", "%{http_code}", "http://example.com/"],
        egress=EgressDecl(hosts=("example.com:80",)),
        limits=Limits(timeout_s=60),
    )
    assert allowed.stdout.strip() == "200", (allowed.stdout, allowed.stderr)
    denied = await sandbox.run(
        [
            CURL,
            "-s",
            "-p",
            "-o",
            "NUL",
            "-m",
            "20",
            "-w",
            "%{http_code}",
            "http://www.wikipedia.org/",
        ],
        egress=EgressDecl(hosts=("example.com:80",)),
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


# ---- the loopback probe fails closed (push review) ---


def _probe_result(stdout: str = "", exit_code: int | None = 0, *, timed_out: bool = False):  # noqa: ANN202
    from nox.sandbox.model import Result

    return Result(exit_code=exit_code, stdout=stdout, stderr="", timed_out=timed_out)


@pytest.mark.parametrize(
    "connected,result,expected",
    [
        (False, "blocked", False),  # the one way to "isolated": explicit report, exit 0
        (True, "blocked", True),  # the core saw the connection: whatever the child says
        (False, "timeout", None),
        (False, "crash", None),
        (False, "silent", None),
        (False, "garbage", None),
        (False, "none", None),  # the probe never produced a result
        (False, "open", True),
    ],
)
def test_probe_verdict_is_isolated_only_on_an_explicit_blocked_report(
    connected: bool, result: str, expected: bool | None
) -> None:
    from nox.sandbox.windows import probe_verdict

    results = {
        "blocked": _probe_result("NOXPROBE BLOCKED\r\n"),
        "timeout": _probe_result("NOXPROBE BLOCKED\r\n", None, timed_out=True),
        "crash": _probe_result("NOXPROBE BLOCKED\r\n", 3221225477),
        "silent": _probe_result(""),
        "garbage": _probe_result("BLOCKED but also something else\r\n"),
        "none": None,
        "open": _probe_result("NOXPROBE OPEN\r\n"),
    }
    assert probe_verdict(connected=connected, result=results[result]) is expected


def test_an_inconclusive_probe_counts_as_reachable_for_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.sandbox import windows

    answers = iter([None, False])
    monkeypatch.setattr(windows, "_probe_loopback", lambda sid: next(answers))
    monkeypatch.setattr(windows, "_loopback_probe", {})
    sid = "S-1-15-2-99-98-97"
    assert windows.host_loopback_reachable(sid) is True  # inconclusive: fail closed
    # ... and consistently so: guarantees() and every later check give the same answer (the
    # hosted runner showed one call weak and the next strong).
    assert windows.host_loopback_reachable(sid) is True
    assert windows._loopback_probe[sid] is True  # noqa: SLF001


async def test_an_inconclusive_probe_means_the_appcontainer_is_not_strong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.sandbox import windows

    if not windows.appcontainer_available():
        pytest.skip("AppContainer profile cannot be created here")
    monkeypatch.setattr(windows, "_probe_loopback", lambda sid: None)
    monkeypatch.setattr(windows, "_loopback_probe", {})
    g = windows.WindowsSandbox(prefer_appcontainer=True).guarantees()
    assert g.confines_network is False and not g.strong
    with pytest.raises(SandboxUnavailable, match="accept_weak_sandbox"):
        await Sandbox(windows.WindowsSandbox(), guard=NetGuard()).run(
            [CMD, "/c", "echo", "x"], limits=Limits(timeout_s=20)
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sandbox")
async def test_appcontainer_runs_python_and_still_cannot_read_user_files(tmp_path) -> None:  # noqa: ANN001
    from nox.sandbox.windows import RuntimeGrants, WindowsSandbox, appcontainer_available

    if not appcontainer_available():
        pytest.skip("AppContainer profile cannot be created here")
    secret = tmp_path / "user-file.txt"  # under the user's profile (TEMP)
    secret.write_text("private", encoding="utf-8")
    script = (
        "import json, email.parser, sqlite3\n"
        "print('IMPORT-OK')\n"
        "try:\n"
        f"    open({str(secret)!r}).read(); print('READ-USER-FILE')\n"
        "except OSError:\n"
        "    print('USER-FILE-BLOCKED')\n"
    )
    grants = RuntimeGrants(tmp_path / "acl-grants.json")
    try:
        backend = WindowsSandbox(prefer_appcontainer=True, grants=grants)
        # Accepted explicitly: on a machine without loopback isolation the AppContainer is not
        # "strong" (see test_appcontainer_guarantees_match_the_measurement).
        result = await Sandbox(backend, guard=NetGuard(), accept_weak=True).run(
            [sys.executable, "-I", "-B", "-c", script], limits=Limits(timeout_s=60)
        )
        assert "IMPORT-OK" in result.stdout, result.stderr
        assert "USER-FILE-BLOCKED" in result.stdout and "READ-USER-FILE" not in result.stdout
        recorded = grants.recorded()
        assert recorded and all(sid == backend._sid for _p, sid in recorded)  # noqa: SLF001
    finally:
        # The low-IL backend resets the recorded grants itself - and must then run Python again.
        low = Sandbox(
            WindowsSandbox(prefer_appcontainer=False, grants=grants),
            guard=NetGuard(),
            accept_weak=True,
        )
    assert grants.recorded() == []
    again = await low.run(
        [sys.executable, "-I", "-B", "-c", "print('LOW-OK')"], limits=Limits(timeout_s=60)
    )
    assert "LOW-OK" in again.stdout, again.stderr


class _Recorder:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.code = code

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return self.code


SID = "S-1-15-2-11-22-33-44-55-66-77"
LEGACY = ("S-1-15-2-1", 0x1200A9, 0x3)  # ALL APPLICATION PACKAGES:(OI)(CI)(RX), explicit


def _acl(table: dict[str, list[tuple[str, int, int]]]):  # noqa: ANN202
    """A fake ACL reader: the explicit allow ACEs per folder (none for an unknown folder)."""

    def read(folder):  # noqa: ANN001, ANN202
        return list(table.get(str(folder), []))

    return read


def test_grants_name_only_the_container_sid_and_are_recorded_once(tmp_path) -> None:  # noqa: ANN001
    from nox.sandbox.windows import RuntimeGrants

    runner = _Recorder()
    grants = RuntimeGrants(tmp_path / "m.json", runner=runner, acl_reader=_acl({}))
    folder = tmp_path / "py"
    folder.mkdir()
    assert grants.grant(folder, SID) and grants.grant(folder, SID)  # a restart: no duplicate
    assert runner.calls == [["icacls", str(folder), "/grant", f"*{SID}:(OI)(CI)(RX)", "/Q"]]
    assert all("S-1-15-2-1:" not in " ".join(c) for c in runner.calls)
    assert grants.recorded() == [(str(folder), SID)]
    for bad in ("S-1-15-2-1", "S-1-5-32-545", "S-1-1-0"):
        with pytest.raises(ValueError):
            grants.grant(folder, bad)


def test_reset_removes_exactly_the_recorded_aces(tmp_path) -> None:  # noqa: ANN001
    from nox.sandbox.windows import RuntimeGrants

    runner = _Recorder()
    grants = RuntimeGrants(tmp_path / "m.json", runner=runner, acl_reader=_acl({}))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    grants.grant(a, SID)
    grants.grant(b, SID)
    runner.calls.clear()
    assert grants.reset() == 2
    assert runner.calls == [
        ["icacls", str(a), "/remove:g", f"*{SID}", "/Q"],
        ["icacls", str(b), "/remove:g", f"*{SID}", "/Q"],
    ]
    assert grants.recorded() == [] and not (tmp_path / "m.json").exists()
    assert grants.reset() == 0  # twice is harmless


def test_a_failed_removal_stays_recorded(tmp_path) -> None:  # noqa: ANN001
    from nox.sandbox.windows import RuntimeGrants

    grants = RuntimeGrants(tmp_path / "m.json", runner=_Recorder(), acl_reader=_acl({}))
    grants.grant(tmp_path, SID)
    failing = RuntimeGrants(tmp_path / "m.json", runner=_Recorder(code=5))
    assert failing.reset() == 0
    assert failing.recorded() == [(str(tmp_path), SID)]


def test_runtime_dirs_never_include_nox_home_or_the_profile(tmp_path) -> None:  # noqa: ANN001
    from pathlib import Path

    from nox.sandbox.windows import python_runtime_dirs

    py = tmp_path / "python"
    venv = tmp_path / "venv"
    home = tmp_path / "venv" / "nox-home"  # a home inside the venv: the venv is refused
    dirs = python_runtime_dirs(home=home, base=str(py), prefix=str(venv))
    assert dirs == (py.resolve(),)
    outer = python_runtime_dirs(
        home=tmp_path / "h", base=str(tmp_path), prefix=str(tmp_path / "h" / "v")
    )
    assert outer == ()  # one contains Nox's home, the other lies inside it
    assert python_runtime_dirs(home=tmp_path / "h", base=str(Path.home()), prefix=str(py)) == (
        py.resolve(),
    )
    broad = Path.home() / "Documents"
    assert python_runtime_dirs(home=tmp_path / "h", base=str(broad), prefix=str(py)) == (
        py.resolve(),
    )


def _old_manifest(path, folders) -> None:  # noqa: ANN001
    """A manifest as the build right after the legacy one wrote it: no pre-state recorded."""
    import json

    grants = [{"path": str(f), "sid": SID} for f in folders]
    path.write_text(json.dumps({"grants": grants}), encoding="utf-8")


def test_the_legacy_broad_ace_is_removed_only_where_nox_can_tell_it_put_it(tmp_path) -> None:  # noqa: ANN001
    # Review finding #18: the migration used to strip ALL APPLICATION PACKAGES from every runtime
    # candidate, including an ACE the user or another program had set there.
    from nox.sandbox.windows import RuntimeGrants

    ours, foreign, odd, never = (tmp_path / n for n in ("ours", "foreign", "odd", "never"))
    for folder in (ours, foreign, odd, never):
        folder.mkdir()
    manifest = tmp_path / "m.json"
    _old_manifest(manifest, [ours, odd])
    table = {
        str(ours): [LEGACY],  # exactly what the legacy build set, on a folder Nox granted
        str(odd): [("S-1-15-2-1", 0x1F01FF, 0x3)],  # full control: not the legacy shape
        str(foreign): [LEGACY],  # same ACE, but Nox never granted this folder
        str(never): [LEGACY],
    }
    runner = _Recorder()
    grants = RuntimeGrants(manifest, runner=runner, acl_reader=_acl(table))
    assert grants.migrate_legacy() == 1
    assert runner.calls == [["icacls", str(ours), "/remove:g", "*S-1-15-2-1", "/Q"]]
    assert grants.migrate_legacy() == 0  # second start: a no-op (marker)
    assert grants.migrate_legacy(force=True) == 0  # both pre-states are known now
    assert len(runner.calls) == 1


def test_a_broad_ace_present_before_noxs_first_grant_is_never_removed(tmp_path) -> None:  # noqa: ANN001
    from nox.sandbox.windows import RuntimeGrants

    folder = tmp_path / "py"
    folder.mkdir()
    runner = _Recorder()
    grants = RuntimeGrants(
        tmp_path / "m.json", runner=runner, acl_reader=_acl({str(folder): [LEGACY]})
    )
    assert grants.grant(folder, SID)
    assert grants.migrate_legacy(force=True) == 0
    assert all("/remove:g" not in call for call in runner.calls)
    grants.reset()  # removes Nox's own container-SID ACE - and only that one
    assert runner.calls[-1] == ["icacls", str(folder), "/remove:g", f"*{SID}", "/Q"]


def test_an_unreadable_acl_is_treated_as_foreign(tmp_path) -> None:  # noqa: ANN001
    from nox.sandbox.windows import RuntimeGrants

    def unreadable(_folder):  # noqa: ANN001, ANN202
        raise OSError("no access")

    folder = tmp_path / "py"
    folder.mkdir()
    runner = _Recorder()
    grants = RuntimeGrants(tmp_path / "m.json", runner=runner, acl_reader=unreadable)
    assert grants.grant(folder, SID)
    assert grants.migrate_legacy(force=True) == 0
    assert all("/remove:g" not in call for call in runner.calls)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_the_acl_reader_reports_explicit_aces_by_sid(tmp_path) -> None:  # noqa: ANN001
    from nox.sandbox.windows import _explicit_aces

    folder = tmp_path / "acl"
    folder.mkdir()
    subprocess.run(  # noqa: S603 - fixed argv on the test's own folder
        ["icacls", str(folder), "/grant", "*S-1-15-2-1:(OI)(CI)(RX)", "/Q"],
        check=True,
        capture_output=True,
    )
    assert LEGACY in _explicit_aces(folder)
    assert all(not ace[0].startswith("S-1-15-2-1") for ace in _explicit_aces(tmp_path))


async def test_a_run_cannot_read_the_work_folder_of_another_workload(tmp_path) -> None:  # noqa: ANN001
    # Re-review N6: every run used one container SID, so a shell command could read the work
    # folder of a running MCP server or skill. Each workload class has its own container now.
    sandbox = _appcontainer()
    backend = sandbox._backend  # type: ignore[attr-defined]
    sid_b = backend.container_sid_for("skill:canary")
    assert sid_b != backend.container_sid_for("shell")
    work_b, _ = backend._prepare_workdir(None, sid_b)
    try:
        # Workload B writes into its own folder, the way a running MCP server or skill does ...
        write = (
            "$f = [System.IO.Path]::Combine($env:TEMP, 'canary.txt'); "
            "[System.IO.File]::WriteAllText($f, 'NOXCANARY-7f3a'); "
            "[Console]::Out.WriteLine([System.IO.File]::ReadAllText($f))"
        )
        own = await sandbox.run(
            [PS, "-NoProfile", "-NonInteractive", "-Command", write],
            cwd=str(work_b),
            limits=Limits(timeout_s=30),
            workload="skill:canary",
        )
        if "NOXCANARY-7f3a" not in own.stdout:
            acl = subprocess.run(  # noqa: ASYNC221 - diagnostics on failure only
                [r"C:\Windows\System32\icacls.exe", str(work_b)],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            ).stdout
            parent = subprocess.run(  # noqa: ASYNC221
                [r"C:\Windows\System32\icacls.exe", str(work_b.parent)],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            ).stdout
            pytest.fail(
                f"own folder not usable\nsid={sid_b}\nstderr={own.stderr[:600]}\n"
                f"acl={acl}\nparent_acl={parent}"
            )
        # ... and a shell command, in a container of its own, cannot read it.
        canary = work_b / "canary.txt"
        read = [
            PS,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"[Console]::Out.WriteLine([System.IO.File]::ReadAllText('{canary}'))",
        ]
        other = await sandbox.run(read, limits=Limits(timeout_s=30), workload="shell")
        assert "NOXCANARY-7f3a" not in other.stdout
        assert other.exit_code != 0 or "denied" in (other.stdout + other.stderr).lower()
    finally:
        import shutil

        shutil.rmtree(work_b, ignore_errors=True)


async def test_a_work_folder_under_a_short_name_is_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    # On the hosted runner TEMP is `C:\Users\RUNNER~1\...`, an 8.3 name. Expanding it means
    # listing the parent folders, which a container may not do - so there every run failed with
    # "access denied" on its own work folder. The backend hands the child the long path.
    import ctypes
    import shutil
    import tempfile
    from pathlib import Path

    parent = Path(tempfile.mkdtemp(prefix="nox-a-long-parent-name-"))
    try:
        buf = ctypes.create_unicode_buffer(1024)
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not kernel32.GetShortPathNameW(str(parent), buf, 1024) or "~" not in buf.value:
            pytest.skip("no 8.3 names on this volume")
        monkeypatch.setattr(tempfile, "tempdir", buf.value)  # what the runner's TEMP looks like
        sandbox = _appcontainer()
        script = (
            "$f = [System.IO.Path]::Combine($env:TEMP, 'canary.txt'); "
            "[System.IO.File]::WriteAllText($f, 'NOXSHORT'); "
            "[Console]::Out.WriteLine([System.IO.File]::ReadAllText($f))"
        )
        own = await sandbox.run(
            [PS, "-NoProfile", "-NonInteractive", "-Command", script],
            limits=Limits(timeout_s=30),
            workload="skill:shortname",
        )
        assert "NOXSHORT" in own.stdout, own.stderr
    finally:
        shutil.rmtree(parent, ignore_errors=True)


async def test_the_system_modules_load_inside_the_container() -> None:
    # On the hosted runner no cmdlet could be found inside the container (`Join-Path` "not
    # recognized"): the default module path names folders the container cannot read.
    sandbox = _appcontainer()
    result = await sandbox.run(
        [PS, "-NoProfile", "-NonInteractive", "-Command", "Join-Path 'a' 'b'"],
        limits=Limits(timeout_s=60),
    )
    if result.stdout.strip() != "a\\b":
        # Why: the module path, what the container can see of each folder, the import error.
        experiments = [
            "$p = $env:PSModulePath; [Console]::Out.WriteLine('PSMP=' + $p); "
            "[Console]::Out.WriteLine('LANG ' + $ExecutionContext.SessionState.LanguageMode); "
            "[Console]::Out.WriteLine('PSHOME ' + $PSHOME + ' ' + $PSVersionTable.PSVersion); "
            "[Console]::Out.WriteLine('MAC ' + $env:PSModuleAnalysisCachePath)",
            r"$env:PSModulePath = $PSHOME + '\Modules'; Join-Path 'a' 'b'",
            "$pf = $env:PSModulePath.Split(';')[0]; "
            "foreach ($m in [IO.Directory]::GetDirectories($pf)) { try { $n = "
            "[IO.Directory]::GetFiles($m, '*.psd1', 'AllDirectories').Length; "
            "[Console]::Out.WriteLine('MOD ' + $m + ' ' + $n) } catch { "
            "[Console]::Out.WriteLine('MOD ' + $m + ' ERR ' + $_.Exception.Message) } }",
            r"Import-Module Microsoft.PowerShell.Utility; $t = $env:TEMP + '\trace.txt'; "
            "Trace-Command -Name CommandDiscovery,Modules -FilePath $t -Expression { "
            "try { Join-Path 'a' 'b' } catch { } }; $k = 0; $all = [IO.File]::ReadAllLines($t); "
            "[Console]::Out.WriteLine('TRLINES ' + $all.Length); "
            "foreach ($l in $all) { if ($k -lt 60 -and $l -match "
            "'exception|error|denied|fail|skip|Found in module|Attempting|not |Management') { "
            "$k++; [Console]::Out.WriteLine('TR ' + $l) } }; "
            "foreach ($l in ($all | Select-Object -Last 25)) { "
            "[Console]::Out.WriteLine('TRLAST ' + $l) }; "
            "[Console]::Out.WriteLine('MACEXISTS ' + "
            "[IO.File]::Exists($env:PSModuleAnalysisCachePath))",
        ]
        report = []
        for script in experiments:
            why = await sandbox.run(
                [PS, "-NoProfile", "-NonInteractive", "-Command", script],
                limits=Limits(timeout_s=120),
            )
            report.append(f"{why.stdout[:6000]}\n{why.stderr[:600]}")
        pytest.fail(f"exe={PS}\n{result.stderr[:300]}\n" + "\n---\n".join(report))
