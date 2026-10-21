"""The external review of C1-C3: every finding has a test here that is red without its fix."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import InvalidStatus

from nox.lan.tls import TlsMaterial
from nox.node import listener as listener_module
from nox.node.listener import NodeListener
from nox.node.pairing import NodePairing
from nox.node.repo import NodeRepository
from nox.security.audit import SqliteAuditLog
from tests.unit.node.conftest import client_context, greet, open_node, pair_node, recv_until


async def _listener(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial, **kwargs: Any
) -> NodeListener:
    server = NodeListener(
        pairing=pairing, nodes=nodes, tls=lambda: tls, host="127.0.0.1", port=0, **kwargs
    )
    await server.start()
    return server


# ---- M2: half-open and slow connections are bounded --------------------------------------------


async def test_a_stalled_tls_handshake_is_dropped(
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener_module, "HANDSHAKE_TIMEOUT_S", 0.5)
    server = await _listener(pairing, nodes, tls)
    try:
        raw = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        raw.settimeout(5)
        started = time.monotonic()
        # Say nothing: no ClientHello. The server must give up on its own, long before 60 s.
        data = await asyncio.to_thread(raw.recv, 1024)
        assert data == b""
        assert time.monotonic() - started < 3.0
        raw.close()
    finally:
        await server.stop()


async def test_one_node_cannot_lock_out_another(
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review M2: a node (or anyone behind the same address) filling the waiting slots only
    displaces its own older attempts; another node still gets in."""
    monkeypatch.setattr(listener_module, "MAX_PREAUTH_TOTAL", 3)
    server = await _listener(pairing, nodes, tls)
    try:
        noisy = await pair_node(pairing, tmp_path)
        legit = await pair_node(pairing, tmp_path)
        held = [await open_node(server, client_context(tls, noisy)) for _ in range(6)]
        waiting = [d for d, _ in server._preauth.values()]
        assert waiting.count(noisy.node_id) == 2  # older attempts were displaced
        ws = await open_node(server, client_context(tls, legit))
        assert (await greet(ws))["result"]["node_id"] == legit.node_id
        await ws.close()
        for h in held:
            await h.close()
    finally:
        await server.stop()


async def test_slow_hellos_are_capped_in_total(
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener_module, "MAX_PREAUTH_TOTAL", 1)
    server = await _listener(pairing, nodes, tls)
    try:
        a = await pair_node(pairing, tmp_path)
        b = await pair_node(pairing, tmp_path)
        held = await open_node(server, client_context(tls, a))
        with pytest.raises(InvalidStatus) as refused:
            await open_node(server, client_context(tls, b))
        assert refused.value.response.status_code == 503
        await held.close()
    finally:
        await server.stop()


# ---- M3: a node repeating security.kill cannot flood the audit trail ---------------------------


async def test_repeats_while_stopped_change_nothing_and_are_audited_once(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial, tmp_path: Path
) -> None:
    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    kills: list[str] = []
    engaged = [False]

    async def on_kill(device_id: str, _reason: str) -> None:
        kills.append(device_id)
        engaged[0] = True

    server = await _listener(
        pairing, nodes, tls, audit=audit, on_security_kill=on_kill, kill_engaged=lambda: engaged[0]
    )
    try:
        node = await pair_node(pairing, tmp_path)
        ws = await open_node(server, client_context(tls, node))
        await greet(ws)
        first = {"jsonrpc": "2.0", "id": 99, "method": "security.kill", "params": {}}
        await ws.send(json.dumps(first))
        assert (await recv_until(ws, lambda m: m.get("id") == 99))["result"]["status"] == "ok"
        for _ in range(100):
            if engaged[0]:
                break
            await asyncio.sleep(0.01)
        statuses = []
        for i in range(30):
            await ws.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 100 + i, "method": "security.kill", "params": {}}
                )
            )
            answer = await recv_until(ws, lambda m, i=i: m.get("id") == 100 + i)
            statuses.append(answer["result"]["status"])
        assert set(statuses) == {"already_engaged"}
        assert kills == [node.node_id]
        audited = [e for e in audit.entries(limit=500) if e.action == "node.security_kill"]
        assert len(audited) == 1  # the stop; repeats within the interval leave no trace
        await ws.close()
    finally:
        await server.stop()


async def test_a_kill_after_a_resume_always_takes_effect(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial, tmp_path: Path
) -> None:
    """Review follow-up: kill -> resume -> an immediate new kill must stop Nox again - a safety
    signal is never throttled away."""
    kills: list[str] = []
    engaged = [False]

    async def on_kill(device_id: str, _reason: str) -> None:
        kills.append(device_id)
        engaged[0] = True

    server = await _listener(
        pairing, nodes, tls, on_security_kill=on_kill, kill_engaged=lambda: engaged[0]
    )
    try:
        node = await pair_node(pairing, tmp_path)
        ws = await open_node(server, client_context(tls, node))
        await greet(ws)
        for i in range(2):
            await ws.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 300 + i, "method": "security.kill", "params": {}}
                )
            )
            answer = await recv_until(ws, lambda m, i=i: m.get("id") == 300 + i)
            assert answer["result"]["status"] == "ok"
            for _ in range(100):
                if engaged[0]:
                    break
                await asyncio.sleep(0.01)
            engaged[0] = False  # the owner pressed "Fortsetzen"
        assert kills == [node.node_id, node.node_id]
        await ws.close()
    finally:
        await server.stop()


# ---- N8: unconsumed blobs do not linger ---------------------------------------------------------


async def test_an_unfinished_blob_is_dropped_after_its_time(
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    server = await _listener(pairing, nodes, tls)
    try:
        node = await pair_node(pairing, tmp_path)
        ws = await open_node(server, client_context(tls, node))
        await greet(ws)

        def blob(call_id: str, msg_id: int) -> str:
            params = {
                "call_id": call_id,
                "seq": 0,
                "last": False,
                "data": base64.b64encode(b"x" * 100).decode(),
            }
            return json.dumps(
                {"jsonrpc": "2.0", "id": msg_id, "method": "nox/blob", "params": params}
            )

        await ws.send(blob("old", 1))
        await recv_until(ws, lambda m: m.get("id") == 1)
        session = server.sessions[node.node_id]
        assert session.blob_bytes == 100
        monkeypatch.setattr(listener_module, "BLOB_TTL_S", 0.0)
        await asyncio.sleep(0.01)
        await ws.send(blob("new", 2))
        await recv_until(ws, lambda m: m.get("id") == 2)
        assert "old" not in session.blobs and session.blob_bytes == 100
        await ws.close()
    finally:
        await server.stop()


async def test_identified_connections_are_held_weakly(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial
) -> None:
    """A connection whose upgrade fails after the certificate check leaves no entry behind."""
    import gc
    import weakref

    server = NodeListener(pairing=pairing, nodes=nodes, tls=lambda: tls, host="127.0.0.1", port=0)
    assert isinstance(server._identified, weakref.WeakKeyDictionary)

    class Conn:
        pass

    conn = Conn()
    server._identified[conn] = ("device", "cert")  # type: ignore[index]
    del conn
    gc.collect()
    assert len(server._identified) == 0


# ---- M5: what this device allows is decided by a person at this device ---------------------------


def _home(tmp_path: Path, *, paired: bool = True) -> Any:
    from nox.node import cli as node_cli
    from nox.node.home import NodeHome

    home = NodeHome(tmp_path / "node-home")
    home.ensure()
    if paired:
        node_cli.confirm_defaults(home, None)
    return home


def _yes(home: Any, group: str) -> None:
    from nox.node import cli as node_cli

    node_cli.allow(
        home, group, True, say=lambda _t: None, interactive=lambda: True, confirm=lambda _p: True
    )


def test_allowing_needs_an_interactive_terminal_and_a_yes(tmp_path: Path) -> None:
    from nox.node import cli as node_cli
    from nox.node.manifest import load_manifest

    home = _home(tmp_path)
    quiet = dict(say=lambda _t: None)
    with pytest.raises(node_cli.NodeCliError, match="interactive"):
        node_cli.allow(
            home, "shell", True, interactive=lambda: False, confirm=lambda _p: True, **quiet
        )
    with pytest.raises(node_cli.NodeCliError, match="Not allowed"):
        node_cli.allow(
            home, "shell", True, interactive=lambda: True, confirm=lambda _p: False, **quiet
        )
    assert not home.manifest_file.exists() or not load_manifest(home.manifest_file).groups["shell"]
    # Taking something away needs no person: narrowing is always safe.
    _yes(home, "notify")
    node_cli.allow(home, "notify", False, interactive=lambda: False, **quiet)
    assert load_manifest(home.manifest_file).groups["notify"] is False


def _effective(home: Any) -> Any:
    from nox.node import cli as node_cli

    return node_cli.build(home, notifier=None).gate.manifest


def test_an_edit_of_node_yaml_widens_nothing(tmp_path: Path) -> None:
    from nox.node.manifest import load_manifest, save_manifest

    home = _home(tmp_path)
    _yes(home, "notify")
    manifest = load_manifest(home.manifest_file)
    manifest.groups["shell"] = True  # written by some process of this user, not confirmed
    manifest.groups["notify"] = False  # ... and a narrowing, which applies
    save_manifest(home.manifest_file, manifest)
    effective = _effective(home)
    assert effective.groups["shell"] is False and effective.groups["notify"] is False


def test_a_forged_confirmation_is_not_trusted(tmp_path: Path) -> None:
    import json as _json

    from nox.node import cli as node_cli
    from nox.node.confirmed import CONFIRM_ACTION
    from nox.node.manifest import GROUPS, NodeManifest, save_manifest

    home = _home(tmp_path)
    _yes(home, "notify")
    everything = NodeManifest(groups={g: True for g in GROUPS})
    save_manifest(home.manifest_file, everything)
    audit = node_cli.open_audit(home)
    for mac in ("", "0" * 64):  # no MAC, a made-up MAC
        audit.append(
            actor="local",
            tool="node",
            action=CONFIRM_ACTION,
            target="",
            decision="allow",
            result="ok",
            details={"manifest": _json.dumps(everything.model_dump()), "mac": mac},
        )
    effective = _effective(home)
    assert effective.enabled() == {"system"} and effective.max_risk == "read"


def test_a_broken_audit_chain_trusts_no_confirmation(tmp_path: Path) -> None:
    from nox.node import cli as node_cli

    home = _home(tmp_path)
    _yes(home, "notify")
    conn = sqlite3.connect(home.audit_file)
    # Someone with the file: the append-only triggers go first, then a row is changed.
    triggers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'audit_log'"
    ).fetchall()
    for (name,) in triggers:
        conn.execute(f'DROP TRIGGER "{name}"')
    conn.execute("UPDATE audit_log SET result = 'tampered' WHERE seq = 1")
    conn.commit()
    conn.close()
    assert node_cli.open_audit(home).verify_chain() is False
    assert _effective(home).enabled() == {"system"}


def test_without_its_key_no_confirmation_counts(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _yes(home, "notify")
    (home.path / "manifest.key").unlink()
    assert _effective(home).enabled() == {"system"}


@pytest.mark.parametrize(
    "where",
    ["manifest.key", "node.yaml", "audit.db", "node.key", "", "sub/../node.key"],
)
def test_no_node_tool_may_touch_the_node_folder(tmp_path: Path, where: str) -> None:
    from pydantic import BaseModel, ConfigDict

    from nox.node.gate import NodeGate, touches
    from nox.node.manifest import GROUPS, NodeManifest
    from nox.node.tools import NodeTool

    home = _home(tmp_path)

    class FileArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        path: str

    async def never(_a: BaseModel) -> dict[str, Any]:
        raise AssertionError("ran")

    tool = NodeTool("file.read", "files", "read", "t", FileArgs, never)
    gate = NodeGate(
        {"file.read": tool},
        NodeManifest(groups={g: True for g in GROUPS}),
        None,
        protected=home.path,
    )
    target = str(home.path / where) if where else str(home.path)
    for variant in (
        target,
        target.upper(),
        target.replace("/", "\\"),
        f'cat "{target}"',
        f"--config={target}",
    ):
        assert touches({"path": variant}, home.path), variant
        assert gate.check("file.read", {"path": variant}).reason == "node_home_protected"
    assert gate.check("file.read", {"path": str(tmp_path / "elsewhere.txt")}).allowed


async def test_deny_takes_effect_in_the_running_node(tmp_path: Path) -> None:
    from nox.node import cli as node_cli
    from nox.node.agent import NodeAgent
    from nox.node.keystore import NodeKeyStore

    home = _home(tmp_path)
    _yes(home, "notify")
    runtime = node_cli.build(home, notifier=None)
    agent = NodeAgent(
        home,
        NodeKeyStore(home.key_file, None),
        runtime.gate,
        runtime.audit,
        manifest_source=node_cli.manifest_source(runtime),
    )
    agent.reload_manifest()
    assert "notify.show" in [t.name for t in runtime.gate.offered()]
    import os

    node_cli.allow(home, "notify", False, say=lambda _t: None)
    stat = home.manifest_file.stat()
    os.utime(home.manifest_file, (stat.st_atime, stat.st_mtime + 5))
    assert agent.reload_manifest() is True
    assert "notify.show" not in [t.name for t in runtime.gate.offered()]


# ---- cheap LOW points --------------------------------------------------------------------------


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows ACLs")
def test_the_node_folder_is_owner_only_on_windows(tmp_path: Path) -> None:
    import subprocess

    home = _home(tmp_path)
    acl = subprocess.run(["icacls", str(home.path)], capture_output=True, text=True, check=False)
    text = acl.stdout
    assert "(OI)(CI)" in text
    assert "(I)" not in text  # nothing inherited from the parent folder any more
    for broad in ("Everyone", "Jeder", "BUILTIN\\Users", "Authenticated Users", "Authentifizierte"):
        assert broad not in text, text


def test_a_node_that_stopped_answering_is_not_offered() -> None:
    from nox.node.registry import NodeEntry

    class Session:
        alive = False

    entry = NodeEntry(device_id="a" * 32, name="Laptop", online=True, tools={"notify.show": "low"})
    entry.session = Session()  # type: ignore[assignment]
    assert entry.offers("notify.show") is False
    Session.alive = True
    assert entry.offers("notify.show") is True


async def test_a_notice_always_says_nox_and_uses_the_systems_powershell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.node import tools as node_tools

    shown: list[tuple[str, str]] = []

    class Fake:
        async def show(self, title: str, text: str) -> None:
            shown.append((title, text))

    notify = node_tools.default_tools(Fake())["notify.show"]
    await notify.handler(node_tools.NotifyArgs(title="Windows Update", text="x"))
    await notify.handler(node_tools.NotifyArgs(text="y"))
    assert shown == [("Nox: Windows Update", "x"), ("Nox", "y")]

    seen: list[list[str]] = []

    class Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_exec(*argv: str, **_kw: Any) -> Proc:
        seen.append(list(argv))
        return Proc()

    monkeypatch.setattr(node_tools.sys, "platform", "win32")
    monkeypatch.setattr(node_tools.asyncio, "create_subprocess_exec", fake_exec)
    await node_tools.PlatformNotifier().show("Nox", "z")
    program = Path(seen[0][0])
    assert program.is_absolute() and program.name.lower() == "powershell.exe"
    assert "system32" in str(program).lower()


def test_an_old_wider_confirmation_appended_again_is_rejected(tmp_path: Path) -> None:
    """Review follow-up: a once valid confirmation appended again (old place, old counter)."""
    from nox.node import cli as node_cli
    from nox.node.confirmed import CONFIRM_ACTION
    from nox.node.manifest import load_manifest, save_manifest

    home = _home(tmp_path)
    _yes(home, "shell")  # a wide confirmation ...
    audit = node_cli.open_audit(home)
    old = [e for e in audit.entries(limit=500) if e.action == CONFIRM_ACTION][-1]
    old_details = audit.details(old.seq)
    node_cli.allow(home, "shell", False, say=lambda _t: None)  # ... later narrowed
    assert _effective(home).groups["shell"] is False
    # Someone appends the old wide confirmation again and widens the file.
    audit.append(
        actor="local",
        tool="node",
        action=CONFIRM_ACTION,
        target="",
        decision="allow",
        result="ok",
        details=old_details,
    )
    manifest = load_manifest(home.manifest_file)
    manifest.groups["shell"] = True
    save_manifest(home.manifest_file, manifest)
    assert _effective(home).enabled() == {"system"}  # nothing trusted: most restrictive


def test_pairing_never_adopts_a_widened_file(tmp_path: Path) -> None:
    from nox.node import cli as node_cli
    from nox.node.manifest import GROUPS, NodeManifest, load_manifest, save_manifest

    home = _home(tmp_path, paired=False)
    save_manifest(
        home.manifest_file,
        NodeManifest(groups={g: True for g in GROUPS}, trust_core_for_high=True),
    )
    start = node_cli.confirm_defaults(home, None)
    assert start.enabled() == {"system", "notify", "media"} and not start.trust_core_for_high
    assert load_manifest(home.manifest_file).enabled() == {"system", "notify", "media"}
    assert _effective(home).enabled() == {"system", "notify", "media"}


def test_a_link_or_short_name_into_the_node_folder_is_still_inside(tmp_path: Path) -> None:
    """Review follow-up: the real path decides - symlinks, junctions and 8.3 names included."""
    import os
    import subprocess
    import sys

    from nox.node.gate import inside, touches

    home = _home(tmp_path)
    secret = home.path / "manifest.key"
    assert secret.exists()
    link = tmp_path / "harmless-looking"
    if sys.platform == "win32":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(home.path)],
            capture_output=True,
            check=False,
        )
        assert made.returncode == 0, made.stderr
    else:
        os.symlink(home.path, link)
    assert inside(link / "manifest.key", home.path)
    assert touches({"path": str(link / "manifest.key")}, home.path)
    if sys.platform == "win32":
        import ctypes

        buffer = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(secret), buffer, 1024):  # type: ignore[attr-defined]
            assert inside(buffer.value, home.path)
    assert not inside(tmp_path / "elsewhere", home.path)
