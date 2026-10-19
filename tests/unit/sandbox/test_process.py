"""`Sandbox.start` / `SandboxProcess`: a long-running confined process with stdio pipes.

The lifecycle logic is tested against a scripted `ProcessIo` on every OS. The real backends are
tested where they exist: Windows here (job object + low-integrity token), Linux `bwrap` in CI.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import psutil
import pytest

from nox.sandbox import (
    EgressDecl,
    MessageTooLarge,
    RestartPolicy,
    Sandbox,
    SandboxProcess,
)
from nox.sandbox.model import Limits, SandboxMode, checked_env
from nox.sandbox.netguard import NetGuard
from nox.sandbox.process import ProcessIo

# ---- the lifecycle, against a scripted process ---


class ScriptedIo:
    """A fake process: stdout/stderr chunks queued by the test, exit decided by the test."""

    def __init__(self, *, exits_on_stdin_close: bool = True) -> None:
        self.out: asyncio.Queue[bytes] = asyncio.Queue()
        self.err: asyncio.Queue[bytes] = asyncio.Queue()
        self.written: list[bytes] = []
        self.exit: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.killed = 0
        self.cleaned = 0
        self.stdin_closed = False
        self._exits_on_close = exits_on_stdin_close

    def end(self, code: int) -> None:
        if not self.exit.done():
            self.exit.set_result(code)
            self.out.put_nowait(b"")
            self.err.put_nowait(b"")

    def io(self) -> ProcessIo:
        async def read_out(_n: int) -> bytes:
            return await self.out.get()

        async def read_err(_n: int) -> bytes:
            return await self.err.get()

        async def write_in(data: bytes) -> None:
            self.written.append(data)

        def close_in() -> None:
            self.stdin_closed = True
            if self._exits_on_close:
                self.end(0)

        async def wait() -> int | None:
            return await self.exit

        def kill() -> None:
            self.killed += 1
            self.end(137)

        async def cleanup() -> None:
            self.cleaned += 1

        return ProcessIo(
            pid=4242,
            read_out=read_out,
            read_err=read_err,
            write_in=write_in,
            close_in=close_in,
            wait=wait,
            kill=kill,
            cleanup=cleanup,
        )


def _process(fake: ScriptedIo, **kwargs) -> SandboxProcess:  # noqa: ANN003
    kwargs.setdefault("max_message_bytes", 64)
    kwargs.setdefault("stderr_bytes", 16)
    return SandboxProcess(fake.io(), **kwargs)


async def test_lines_are_reassembled_across_chunks() -> None:
    fake = ScriptedIo()
    proc = _process(fake)
    for chunk in (b'{"a"', b":1}\n{", b'"b":2}\n'):
        fake.out.put_nowait(chunk)
    assert await proc.readline() == b'{"a":1}\n'
    assert await proc.readline() == b'{"b":2}\n'
    fake.end(0)
    assert await proc.readline() == b""
    assert await proc.wait(5) == 0


async def test_an_overlong_line_raises_and_the_next_line_still_reads() -> None:
    fake = ScriptedIo()
    proc = _process(fake, max_message_bytes=8)
    fake.out.put_nowait(b"x" * 20)
    fake.out.put_nowait(b"yyy\nok\n")
    with pytest.raises(MessageTooLarge):
        await proc.readline()
    assert await proc.readline() == b"ok\n"
    await proc.stop(0.1)


async def test_an_overlong_last_line_without_newline_still_raises() -> None:
    fake = ScriptedIo()
    proc = _process(fake, max_message_bytes=8)
    fake.out.put_nowait(b"x" * 20)
    await asyncio.sleep(0.01)
    fake.end(0)
    with pytest.raises(MessageTooLarge):
        await proc.readline()
    assert await proc.readline() == b""


async def test_stderr_is_a_bounded_ring() -> None:
    fake = ScriptedIo()
    proc = _process(fake, stderr_bytes=8)
    for chunk in (b"aaaa", b"bbbb", b"cccc"):
        fake.err.put_nowait(chunk)
    for _ in range(50):
        if "cccc" in proc.stderr_tail():
            break
        await asyncio.sleep(0.01)
    assert proc.stderr_tail() == "bbbbcccc"
    await proc.stop(0.1)


async def test_stop_closes_stdin_and_waits_for_a_clean_exit() -> None:
    fake = ScriptedIo(exits_on_stdin_close=True)
    proc = _process(fake)
    assert await proc.stop(5) == 0
    assert fake.stdin_closed and fake.killed == 1  # the tree is swept after the exit, too
    assert fake.cleaned == 1


async def test_stop_kills_a_process_that_ignores_stdin_close() -> None:
    fake = ScriptedIo(exits_on_stdin_close=False)
    exits: list[int | None] = []
    proc = _process(fake, on_exit=exits.append)
    assert await proc.stop(0.05) is None  # killed: no exit code of its own
    assert fake.killed >= 1
    assert exits == [None]
    assert not proc.running


async def test_lifetime_kills_the_process() -> None:
    fake = ScriptedIo(exits_on_stdin_close=False)
    proc = _process(fake, lifetime_s=0.05)
    assert await proc.wait(5) is None
    assert fake.killed >= 1


async def test_send_after_exit_is_a_broken_pipe() -> None:
    fake = ScriptedIo()
    proc = _process(fake)
    await proc.send(b"hi\n")
    assert fake.written == [b"hi\n"]
    fake.end(3)
    assert await proc.wait(5) == 3
    with pytest.raises(BrokenPipeError):
        await proc.send(b"again\n")


async def test_on_exit_runs_once_even_if_it_raises() -> None:
    fake = ScriptedIo()
    calls: list[int | None] = []

    def hook(code: int | None) -> None:
        calls.append(code)
        raise RuntimeError("supervisor bug")

    proc = _process(fake, on_exit=hook)
    fake.end(1)
    assert await proc.wait(5) == 1
    await proc.stop(0.1)
    assert calls == [1]


async def test_an_unread_stdout_does_not_block_the_exit() -> None:
    fake = ScriptedIo()
    proc = _process(fake)
    for _ in range(200):  # more than the queue holds: the pump parks on a full queue
        fake.out.put_nowait(b"line\n")
    await asyncio.sleep(0.05)
    fake.end(0)
    assert await proc.wait(5) == 0
    assert await proc.readline() == b"line\n"  # what was queued stays readable


def test_restart_policy_backs_off_and_gives_up() -> None:
    policy = RestartPolicy(max_restarts=3, backoff_s=(1.0, 5.0))
    assert [policy.delay_for(n) for n in (1, 2, 3, 4)] == [1.0, 5.0, 5.0, None]
    assert policy.delay_for(0) is None
    assert RestartPolicy(max_restarts=0).delay_for(1) is None


@pytest.mark.parametrize(
    "key", ["PATH", "path", "HTTPS_PROXY", "http_proxy", "HOME", "USERPROFILE", "LD_PRELOAD"]
)
def test_reserved_env_keys_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match="sandbox owns"):
        checked_env({key: "x"})


def test_ordinary_env_passes_and_bad_names_are_refused() -> None:
    assert checked_env({"MCP_TOKEN_FILE": "t"}) == {"MCP_TOKEN_FILE": "t"}
    assert checked_env(None) == {}
    with pytest.raises(ValueError, match="invalid"):
        checked_env({"A=B": "x"})


async def test_start_refuses_a_reserved_env_key_before_starting_anything() -> None:
    class Exploding:
        mode = None

        def guarantees(self):  # noqa: ANN202
            raise AssertionError

        async def start(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError("must not start")

    sandbox = Sandbox(Exploding(), guard=NetGuard())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await sandbox.start(["x"], env={"HTTPS_PROXY": "http://evil:1"})


async def test_the_egress_proxy_lives_as_long_as_the_process() -> None:
    fake = ScriptedIo()
    seen: dict[str, object] = {}

    class Backend:
        mode = SandboxMode.LINUX_BWRAP

        async def start(self, argv, **kw):  # noqa: ANN001, ANN003, ANN202
            seen["port"] = kw["proxy_port"]
            seen["auth"] = kw["proxy_auth"]
            return fake.io()

    sandbox = Sandbox(Backend(), guard=NetGuard())  # type: ignore[arg-type]
    proc = await sandbox.start(["srv"], egress=EgressDecl(hosts=("example.com:443",)))
    port = seen["port"]
    assert isinstance(port, int) and port > 0
    reader, writer = await asyncio.open_connection("127.0.0.1", port)  # the proxy is up
    # ...but serves only the child it was made for: no credentials, no egress.
    writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    await writer.drain()
    assert (await asyncio.wait_for(reader.read(), 10)).startswith(b"HTTP/1.1 407")
    writer.close()
    user, _, secret = str(seen["auth"]).partition(":")
    assert user and len(secret) >= 32, "each run's proxy carries its own random secret"
    await proc.stop(1)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)  # and gone with the process


# ---- the real Windows backend ---

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows sandbox")

ECHO_SERVER = (
    "import os, sys\n"
    "sys.stderr.write('ready\\n'); sys.stderr.flush()\n"
    "for line in sys.stdin:\n"
    "    reply = os.environ.get(line.strip(), 'echo:' + line.strip())\n"
    "    sys.stdout.write(reply + '\\n'); sys.stdout.flush()\n"
)


def _windows_sandbox() -> Sandbox:
    from nox.sandbox.windows import WindowsSandbox

    return Sandbox(WindowsSandbox(prefer_appcontainer=False), guard=NetGuard())


@windows_only
async def test_windows_echo_server_round_trip_with_env() -> None:
    proc = await _windows_sandbox().start(
        [sys.executable, "-I", "-B", "-u", "-c", ECHO_SERVER],
        env={"NOX_TEST_VALUE": "from-env"},
        limits=Limits(memory_bytes=512 * 1024 * 1024),
    )
    async with proc:
        await proc.send(b"hello\n")
        # Text-mode stdout on Windows ends lines with CRLF; the line framing is what matters.
        assert (await asyncio.wait_for(proc.readline(), 30)).rstrip() == b"echo:hello"
        await proc.send(b"NOX_TEST_VALUE\n")
        assert (await asyncio.wait_for(proc.readline(), 30)).rstrip() == b"from-env"
        # The core's own environment is not inherited.
        await proc.send(b"USERNAME\n")
        assert (await asyncio.wait_for(proc.readline(), 30)).rstrip() == b"echo:USERNAME"
    assert not proc.running
    assert proc.returncode == 0  # it saw stdin close and exited on its own
    assert "ready" in proc.stderr_tail()


@windows_only
async def test_windows_stop_kills_the_whole_tree(tmp_path: Path) -> None:
    grandchild = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(600)\n"
    )
    proc = await _windows_sandbox().start([sys.executable, "-I", "-B", "-u", "-c", grandchild])
    child_pid = int((await asyncio.wait_for(proc.readline(), 30)).strip())
    assert psutil.pid_exists(child_pid)
    await proc.stop(grace_s=0.2)
    assert proc.returncode is None  # killed, not exited
    for pid in (proc.pid, child_pid):
        for _ in range(200):
            if not psutil.pid_exists(pid):
                break
            await asyncio.sleep(0.05)
        assert not psutil.pid_exists(pid), f"{pid} survived the stop"


@windows_only
async def test_windows_started_process_has_no_network() -> None:
    probe = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=5); print('OPEN')\n"
        "except OSError:\n"
        "    print('BLOCKED')\n"
    )
    async with await _windows_sandbox().start([sys.executable, "-I", "-B", "-u", "-c", probe]) as p:
        assert (await asyncio.wait_for(p.readline(), 30)).strip() == b"BLOCKED"


@windows_only
async def test_windows_lifetime_ends_a_running_server() -> None:
    proc = await _windows_sandbox().start(
        [sys.executable, "-I", "-B", "-c", "import time; time.sleep(600)"], lifetime_s=0.5
    )
    assert await proc.wait(30) is None


# ---- the real Linux backend (CI) ---


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux bwrap sandbox",
)
async def test_linux_echo_server_round_trip() -> None:
    from nox.sandbox.linux import LinuxSandbox

    sandbox = Sandbox(LinuxSandbox(), guard=NetGuard())
    script = 'while read line; do echo "echo:$line"; done'
    try:
        proc = await sandbox.start(["/bin/sh", "-c", script])
    except Exception as exc:  # noqa: BLE001 - an unprivileged CI runner may not allow userns
        pytest.skip(f"bwrap cannot run here: {exc}")
    async with proc:
        await proc.send(b"hi\n")
        line = await asyncio.wait_for(proc.readline(), 30)
        if not line:
            pytest.skip(f"bwrap refused to start: {proc.stderr_tail()}")
        assert line == b"echo:hi\n"
    assert not proc.running


@windows_only
async def test_windows_child_gets_its_own_proxy_credentials() -> None:
    sandbox = _windows_sandbox()
    echo = [r"C:\Windows\System32\cmd.exe", "/c", "echo %HTTPS_PROXY%"]
    egress = EgressDecl(hosts=("example.com:443",))
    first = await sandbox.run(echo, egress=egress, limits=Limits(timeout_s=20))
    url = first.stdout.strip()
    assert url.startswith("http://nox:") and "@127.0.0.1:" in url
    second = await sandbox.run(echo, egress=egress, limits=Limits(timeout_s=20))
    assert second.stdout.strip() != url, "a fresh secret (and port) per run"


@windows_only
async def test_windows_proxy_secret_is_never_on_the_command_line() -> None:
    """CreateProcess gets the secret in the environment block only; the command line is
    readable by other processes, the environment of a low-IL child is not handed out."""
    proc = await _windows_sandbox().start(
        [sys.executable, "-I", "-B", "-u", "-c", ECHO_SERVER],
        egress=EgressDecl(hosts=("example.com:443",)),
        env={"MCP_TOKEN": "tok-12345"},
    )
    async with proc:
        await proc.send(b"HTTPS_PROXY\n")
        url = (await asyncio.wait_for(proc.readline(), 30)).strip().decode()
        secret = url.split(":", 2)[2].split("@", 1)[0]
        assert url.startswith("http://nox:") and len(secret) >= 32
        cmdline = " ".join(psutil.Process(proc.pid).cmdline())
        assert secret not in cmdline and "tok-12345" not in cmdline
