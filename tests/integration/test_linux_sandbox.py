"""The real Linux sandbox (bwrap) - what its network and its ceilings actually allow.

Runs only on Linux with a working `bwrap`; CI installs it and sets `NOX_REQUIRE_BWRAP=1`, so there
a missing or broken bwrap is a failure, not a silent skip. Everything is local - no internet:

* a TCP server on the host (bound to every interface) stands in for "services on this machine
  and in the LAN";
* "a declared public host" is a name the run's proxy resolves to a public address and then
  dials - in the test, onto that same local server. So reaching it proves the path
  child → in-sandbox forwarder → Unix socket → proxy policy → upstream, while the direct
  attempts prove the child itself reaches nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import socket
import sys
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from nox.sandbox import EgressDecl, EgressProxy, Limits, Sandbox, SandboxUnavailable
from nox.sandbox.netguard import NetGuard

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sandbox")

MARKER = "hello-from-upstream"
PY = "/usr/bin/python3"

CHILD = r"""
import base64, json, os, socket, sys, urllib.parse

def recv_head(s):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf

def proxy_get(target, auth=True):
    u = urllib.parse.urlsplit(os.environ["HTTPS_PROXY"])
    s = socket.create_connection((u.hostname, u.port), 10)
    req = "CONNECT %s HTTP/1.1\r\nHost: %s\r\n" % (target, target)
    if auth:
        token = base64.b64encode(("%s:%s" % (u.username, u.password)).encode()).decode()
        req += "Proxy-Authorization: Basic %s\r\n" % token
    s.sendall((req + "\r\n").encode())
    status = recv_head(s).split(b"\r\n", 1)[0].decode("utf-8", "replace")
    body = b""
    if " 200 " in status:
        s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            body += chunk
    s.close()
    return {"status": status, "body": body.decode("utf-8", "replace")}

def direct(host, port):
    try:
        socket.create_connection((host, port), 3).close()
        return "connected"
    except OSError as exc:
        return type(exc).__name__

def do(op):
    if op["op"] == "proxy_get":
        return proxy_get(op["target"], op.get("auth", True))
    if op["op"] == "direct":
        return direct(op["host"], op["port"])
    return sorted(k for k in os.environ if "proxy" in k.lower())

if len(sys.argv) > 1:
    print(json.dumps([do(op) for op in json.loads(sys.argv[1])]))
else:
    for line in sys.stdin:
        print(json.dumps(do(json.loads(line))), flush=True)
"""


@pytest.fixture(scope="module")
def backend() -> Iterator[Any]:
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux sandbox")
    from nox.sandbox.linux import LinuxSandbox

    try:
        yield LinuxSandbox()
    except SandboxUnavailable as exc:
        if os.environ.get("NOX_REQUIRE_BWRAP") == "1":
            pytest.fail(f"bwrap must work in this job: {exc}")
        pytest.skip(f"no working bwrap here: {exc}")


@pytest.fixture
async def host_server() -> AsyncIterator[int]:
    """A TCP server on every interface of the host: loopback services and the LAN in one."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            writer.write(f"HTTP/1.0 200 OK\r\n\r\n{MARKER}".encode())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", 0)  # noqa: S104 - the point of the test
    yield int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()


def _lan_ip() -> str | None:
    """This machine's address in its network (no packet is sent), or None without one."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
        except OSError:
            return None
        ip = str(s.getsockname()[0])
    return None if ip.startswith("127.") else ip


async def _reachable_from_host(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    except OSError:
        return False
    writer.close()
    return True


async def _run(sandbox: Sandbox, ops: list[dict[str, Any]], egress: EgressDecl) -> list[Any]:
    result = await sandbox.run(
        [PY, "-I", "-c", CHILD, json.dumps(ops)], egress=egress, limits=Limits(timeout_s=60)
    )
    assert result.ok, f"exit {result.exit_code}: {result.stderr}"
    return list(json.loads(result.stdout))


def _routed_sandbox(backend: Any, monkeypatch: pytest.MonkeyPatch, upstream_port: int) -> Sandbox:
    """A sandbox whose proxies resolve every name to a public address and dial the local server.

    Policy (credentials, declaration, NetGuard on the resolved address) is the real one; only the
    last hop is redirected, so the test needs no internet.
    """
    sandbox = Sandbox(backend, guard=NetGuard())

    async def dial(_host: str, _port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    def new_proxy(egress: EgressDecl) -> EgressProxy:
        return EgressProxy(
            allow=egress.hosts,
            guard=NetGuard(),
            resolver=lambda _h, _p: [ipaddress.ip_address("93.184.215.14")],
            open_connection=dial,
            credentials=EgressProxy.new_credentials(),
        )

    monkeypatch.setattr(sandbox, "_new_proxy", new_proxy)
    return sandbox


def _direct_ops(port: int, lan: str | None) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [
        {"op": "direct", "host": "127.0.0.1", "port": port},
        {"op": "direct", "host": "127.0.0.1", "port": 47801},  # Nox's own API port
    ]
    if lan:
        ops.append({"op": "direct", "host": lan, "port": port})
    return ops


async def test_without_egress_nothing_is_reachable(backend: Any, host_server: int) -> None:
    lan = _lan_ip()
    assert await _reachable_from_host("127.0.0.1", host_server)
    if lan:
        assert await _reachable_from_host(lan, host_server), "the LAN check would prove nothing"
    sandbox = Sandbox(backend, guard=NetGuard())
    ops = [
        {"op": "env"},
        *_direct_ops(host_server, lan),
        {"op": "direct", "host": "127.0.0.1", "port": 3128},  # no forwarder without egress
        {"op": "direct", "host": "1.1.1.1", "port": 443},
    ]
    env, *directs = await _run(sandbox, ops, EgressDecl())
    assert env == [], "no proxy variables without declared egress"
    assert "connected" not in directs, directs


async def test_declared_host_is_reachable_only_through_the_proxy(
    backend: Any, host_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    lan = _lan_ip()
    sandbox = _routed_sandbox(backend, monkeypatch, host_server)
    ops = [
        {"op": "proxy_get", "target": "example.test:80"},
        {"op": "proxy_get", "target": "undeclared.test:80"},
        {"op": "proxy_get", "target": "example.test:81"},
        {"op": "proxy_get", "target": "example.test:80", "auth": False},
        *_direct_ops(host_server, lan),
    ]
    declared, undeclared, other_port, no_auth, *directs = await _run(
        sandbox, ops, EgressDecl(hosts=("example.test:80",))
    )
    assert " 200 " in declared["status"] and MARKER in declared["body"], declared
    assert " 403 " in undeclared["status"], undeclared
    assert " 403 " in other_port["status"], other_port
    assert " 407 " in no_auth["status"], no_auth
    # The forwarder is the only way out: the host's loopback and the LAN stay unreachable.
    assert "connected" not in directs, directs


async def test_even_a_wildcard_cannot_reach_the_host_or_the_lan(
    backend: Any, host_server: int
) -> None:
    """The real proxy with `*`: NetGuard still refuses loopback and private addresses."""
    lan = _lan_ip()
    sandbox = Sandbox(backend, guard=NetGuard())
    targets = [f"127.0.0.1:{host_server}", f"localhost:{host_server}"]
    if lan and ipaddress.ip_address(lan).is_private:
        targets.append(f"{lan}:{host_server}")
    ops = [{"op": "proxy_get", "target": t} for t in targets]
    for target, answer in zip(
        targets, await _run(sandbox, ops, EgressDecl(hosts=("*",))), strict=True
    ):
        assert " 403 " in answer["status"], (target, answer)
        assert MARKER not in answer["body"]


async def test_a_long_running_process_uses_the_same_path(
    backend: Any, host_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _routed_sandbox(backend, monkeypatch, host_server)
    proc = await sandbox.start(
        [PY, "-I", "-u", "-c", CHILD], egress=EgressDecl(hosts=("example.test:80",))
    )
    async with proc:

        async def ask(op: dict[str, Any]) -> Any:
            await proc.send((json.dumps(op) + "\n").encode())
            return json.loads(await asyncio.wait_for(proc.readline(), 30))

        first = await ask({"op": "proxy_get", "target": "example.test:80"})
        assert MARKER in first["body"], (first, proc.stderr_tail())
        assert (await ask({"op": "direct", "host": "127.0.0.1", "port": host_server})) != (
            "connected"
        )
        second = await ask({"op": "proxy_get", "target": "example.test:80"})
        assert MARKER in second["body"]


def _expect_ceiling(backend: Any) -> None:
    expected = os.environ.get("NOX_EXPECT_CEILING")
    if expected:
        assert backend.ceiling.value == expected
    if backend.ceiling.value == "none":
        pytest.skip("no ceiling mechanism on this machine")


async def test_the_memory_ceiling_kills_the_process(backend: Any) -> None:
    _expect_ceiling(backend)
    sandbox = Sandbox(backend, guard=NetGuard())
    code = "b = b'x' * (512 << 20)\nprint('survived', len(b))\n"
    result = await sandbox.run(
        [PY, "-I", "-c", code], limits=Limits(memory_bytes=96 << 20, timeout_s=60)
    )
    assert not result.ok and "survived" not in result.stdout, result


async def test_the_process_ceiling_refuses_more_processes(backend: Any) -> None:
    _expect_ceiling(backend)
    sandbox = Sandbox(backend, guard=NetGuard())
    code = (
        "import subprocess\n"
        "procs = []\n"
        "try:\n"
        "    for _ in range(40):\n"
        "        procs.append(subprocess.Popen(['sleep', '5']))\n"
        "    print('spawned all')\n"
        "except OSError as exc:\n"
        "    print('refused', type(exc).__name__)\n"
        "for p in procs:\n"
        "    p.kill()\n"
    )
    result = await sandbox.run([PY, "-I", "-c", code], limits=Limits(max_processes=8, timeout_s=60))
    assert "refused" in result.stdout, result
