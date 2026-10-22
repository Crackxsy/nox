"""C5: the node's hands - `file.*` and `shell.run` - each with its own boundary.

File tools decide on the real file at open time (`nox.node.safe_fs`): every escape below is
tried for real on this OS (junctions on Windows, symlinks elsewhere), and a swap between the text
check and the open is staged. Shell commands run only in the node's sandbox; without one
they are refused, never run unconfined. All of it reaches the hands only through `NodeGate`, the
confirmation on the device and the core's receipts (driven over the real listener)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nox.lan.tls import TlsMaterial
from nox.node.agent import NodeAgent
from nox.node.confirmed import MOST_RESTRICTIVE, effective_manifest, record_confirmed
from nox.node.gate import NodeGate
from nox.node.hands import NodeHands, PathArgs, WriteArgs, hand_tools
from nox.node.home import NodeHome, NodeIdentity
from nox.node.install import _shell_target
from nox.node.keystore import NodeKeyStore
from nox.node.listener import NodeListener
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.pairing import NodePairing
from nox.node.protocol import NoxCallMeta
from nox.node.registry import ROUTABLE, NodeRegistry
from nox.node.router import routed_spec
from nox.node.safe_fs import (
    Folders,
    OutsideFolders,
    list_folder,
    open_existing,
    open_new,
    secret_locations,
)
from nox.node.tools import NodeRefusal
from nox.sandbox import Result, SandboxMode, SandboxUnavailable
from nox.security.audit import SqliteAuditLog
from nox.security.model import Risk
from tests.unit.node.conftest import key_pem, pair_node
from tests.unit.node.test_c4 import TARGET, Confirmer, call, session_of, signed
from tests.unit.node.test_router import FakeRegistry, _entry

WINDOWS = sys.platform == "win32"


def link_folder(target: Path, link: Path) -> None:
    """A link to a folder the way this OS offers one without privileges."""
    if WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "allowed"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "einkauf.txt").write_bytes(b"Haferflocken\n")
    node = tmp_path / "node-home"
    node.mkdir()
    (node / "node.key").write_text("NODE-KEY", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "private.txt").write_text("not for Nox", encoding="utf-8")
    return {"root": root, "node": node, "outside": outside}


def folders(world: dict[str, Path]) -> Folders:
    return Folders.build([str(world["root"])], [world["node"]])


def read(f: Folders, text: str) -> bytes:
    fd = open_existing(f, text)
    try:
        return os.read(fd, 1000)
    finally:
        os.close(fd)


def refused(f: Folders, text: str) -> str:
    with pytest.raises(OutsideFolders) as caught:
        read(f, text)
    return caught.value.reason


# ---- the text ------------------------------------------------------------------------------------


def test_inside_an_allowed_folder_relative_and_absolute(world: dict[str, Path]) -> None:
    f = folders(world)
    assert read(f, "notes/einkauf.txt") == b"Haferflocken\n"
    assert read(f, str(world["root"] / "notes" / "einkauf.txt")) == b"Haferflocken\n"


@pytest.mark.parametrize(
    "text",
    [
        "../node-home/node.key",
        "notes/../../node-home/node.key",
        "~/x.txt",
        "NOXNOD~1/node.key",
        "\\\\fileserver\\share\\x.txt",
        "//fileserver/share/x.txt",
        "\\\\?\\C:\\Windows\\win.ini",
        "\\\\.\\pipe\\nox",
        "notes/a\x00b",
    ],
)
def test_aliases_network_and_device_paths_are_refused_unresolved(
    world: dict[str, Path], text: str
) -> None:
    assert refused(folders(world), text) in ("path_alias_refused", "invalid_path")


@pytest.mark.skipif(not WINDOWS, reason="Windows names")
@pytest.mark.parametrize("text", ["notes/a.txt:hidden", "CON", "notes/nul.txt", "notes/x.", "C:x"])
def test_windows_streams_devices_and_ambiguous_names_are_refused(
    world: dict[str, Path], text: str
) -> None:
    assert refused(folders(world), text) == "invalid_path"


@pytest.mark.skipif(not WINDOWS, reason="Windows long paths")
def test_a_resolved_long_path_is_compared_without_its_prefix(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node.safe_fs import _plain

    assert _plain("\\\\?\\C:\\data") == "C:\\data"
    assert _plain("\\\\?\\UNC\\fileserver\\share") == "\\\\?\\UNC\\fileserver\\share"
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p: "\\\\?\\" + real(p))
    long_form = Folders.build([str(world["root"])], [world["node"]])
    assert read(long_form, "notes/einkauf.txt") == b"Haferflocken\n"


def test_an_absolute_path_outside_and_no_folder_at_all(world: dict[str, Path]) -> None:
    assert refused(folders(world), str(world["outside"] / "private.txt")) == "outside_folders"
    assert refused(Folders.build([], [world["node"]]), "notes/einkauf.txt") == "no_folders"


def test_a_folder_inside_the_node_folder_never_counts(world: dict[str, Path]) -> None:
    inner = world["node"] / "inner"
    inner.mkdir()
    assert Folders.build([str(inner)], [world["node"]]).roots == ()


# ---- the open, on the handle ---------------------------------------------------------------------


def test_a_link_to_the_node_folder_is_refused(world: dict[str, Path]) -> None:
    link_folder(world["node"], world["root"] / "door")
    reason = refused(folders(world), "door/node.key")
    assert reason in ("node_home_protected", "link_refused")


def test_a_link_out_of_the_allowed_folder_is_refused(world: dict[str, Path]) -> None:
    link_folder(world["outside"], world["root"] / "out")
    assert refused(folders(world), "out/private.txt") in ("outside_folders", "link_refused")


def test_a_new_file_through_a_link_is_never_left_behind(world: dict[str, Path]) -> None:
    link_folder(world["node"], world["root"] / "door")
    with pytest.raises(OutsideFolders):
        open_new(folders(world), "door/planted.txt")
    assert not (world["node"] / "planted.txt").exists()


def test_a_hard_link_to_a_protected_file_is_refused(world: dict[str, Path]) -> None:
    os.link(world["node"] / "node.key", world["root"] / "copy.key")
    assert refused(folders(world), "copy.key") == "node_home_protected"


def test_the_node_folder_inside_an_allowed_folder_stays_closed(world: dict[str, Path]) -> None:
    parent = world["root"].parent  # contains both the allowed folder and the node folder
    f = Folders.build([str(parent)], [world["node"]])
    assert read(f, "allowed/notes/einkauf.txt") == b"Haferflocken\n"
    assert refused(f, "node-home/node.key") == "node_home_protected"
    with pytest.raises(OutsideFolders):
        with list_folder(f, "node-home"):
            pass


def test_keyring_folders_are_protected(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = world["root"].parent / "appdata"
    monkeypatch.setenv("APPDATA", str(data))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    store = data / ("Microsoft/Credentials" if WINDOWS else "keyrings")
    store.mkdir(parents=True)
    (store / "login.keyring").write_text("x", encoding="utf-8")
    # A link into the store from an allowed folder is refused on the handle.
    link_folder(store, world["root"] / "door")
    hands = NodeHands(
        folders=lambda: [str(world["root"])], protected=(world["node"],), sandbox=lambda: None
    )
    reason = refused(hands.resolved(), "door/login.keyring")
    assert reason in ("node_home_protected", "link_refused")
    if WINDOWS:  # a folder that contains the app-data root is too broad to allow at all
        assert Folders.build([str(data.parent)], [world["node"]]).roots == ()
    for place in (".ssh", ".aws", ".kube", ".git-credentials", ".netrc", ".pgpass"):
        assert Path.home() / place in secret_locations()


def test_a_swap_between_the_text_check_and_the_open_is_caught(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    f = folders(world)
    real_split = Folders.split

    def split_then_swap(self: Folders, text: str) -> Any:
        found = real_split(self, text)
        # The text was fine: `notes` was a real folder. Now it becomes a door to the node folder.
        os.rename(world["root"] / "notes", world["root"] / "notes-old")
        link_folder(world["node"], world["root"] / "notes")
        (world["node"] / "einkauf.txt").write_text("NODE-SECRET", encoding="utf-8")
        return found

    monkeypatch.setattr(Folders, "split", split_then_swap)
    with pytest.raises(OutsideFolders) as caught:
        read(f, "notes/einkauf.txt")
    assert caught.value.reason in ("node_home_protected", "link_refused")


def test_after_the_open_the_handle_is_what_counts(world: dict[str, Path]) -> None:
    fd = open_existing(folders(world), "notes/einkauf.txt")
    try:
        target = world["root"] / "notes" / "einkauf.txt"
        if WINDOWS:
            with pytest.raises(PermissionError):  # opened without FILE_SHARE_DELETE
                os.replace(world["outside"] / "private.txt", target)
        else:
            os.replace(world["outside"] / "private.txt", target)
        assert os.read(fd, 100) == b"Haferflocken\n"
    finally:
        os.close(fd)


def test_a_listing_never_follows_links(world: dict[str, Path]) -> None:
    link_folder(world["outside"], world["root"] / "out")
    with list_folder(folders(world), ".") as entries:
        names = {e.name: e.is_symlink() or e.is_junction() for e in entries}
    assert set(names) == {"notes", "out"}


async def test_a_listing_reports_junctions_and_links_as_links(world: dict[str, Path]) -> None:
    link_folder(world["outside"], world["root"] / "out")
    listing = await _hands(world)["file.list"].handler(PathArgs(path="."))
    kinds = {e["name"]: e["kind"] for e in listing["entries"]}
    assert kinds == {"notes": "folder", "out": "link"}


# ---- the file tools ----------------------------------------------------------------------------


def _hands(world: dict[str, Path], sandbox: Any = None) -> dict[str, Any]:
    hands = NodeHands(
        folders=lambda: [str(world["root"])], protected=(world["node"],), sandbox=lambda: sandbox
    )
    return hand_tools(hands)


async def test_write_never_overwrites_unless_asked_and_never_makes_folders(
    world: dict[str, Path],
) -> None:
    tools = _hands(world)
    write = tools["file.write"].handler
    new = await write(WriteArgs(path="notes/neu.txt", text="eins"))
    assert new["ok"] is True
    again = await write(WriteArgs(path="notes/neu.txt", text="zwei"))
    assert again["ok"] is False
    await write(WriteArgs(path="notes/neu.txt", text="+", append=True))
    assert (world["root"] / "notes" / "neu.txt").read_text(encoding="utf-8") == "eins+"
    await write(WriteArgs(path="notes/neu.txt", text="drei", overwrite=True))
    assert (world["root"] / "notes" / "neu.txt").read_text(encoding="utf-8") == "drei"
    missing = await write(WriteArgs(path="nowhere/x.txt", text="x"))
    assert missing["ok"] is False and not (world["root"] / "nowhere").exists()


async def test_an_overwrite_through_a_link_truncates_nothing(world: dict[str, Path]) -> None:
    link_folder(world["node"], world["root"] / "door")
    with pytest.raises(NodeRefusal):
        await _hands(world)["file.write"].handler(
            WriteArgs(path="door/node.key", text="", overwrite=True)
        )
    assert (world["node"] / "node.key").read_text(encoding="utf-8") == "NODE-KEY"


async def test_read_and_list_answer_as_data(world: dict[str, Path]) -> None:
    tools = _hands(world)
    text = await tools["file.read"].handler(PathArgs(path="notes/einkauf.txt"))
    assert text["text"] == "Haferflocken\n"
    listing = await tools["file.list"].handler(PathArgs(path="notes"))
    assert [e["name"] for e in listing["entries"]] == ["einkauf.txt"]
    with pytest.raises(NodeRefusal) as caught:
        await tools["file.read"].handler(PathArgs(path="../node-home/node.key"))
    assert caught.value.reason == "path_alias_refused"


# ---- shell and packages: only in the sandbox -----------------------------------------------------


class FakeSandbox:
    """Records what would run; strong unless told otherwise."""

    mode = SandboxMode.LINUX_BWRAP

    def __init__(self, *, weak: bool = False, sleep: float = 0.0) -> None:
        self.runs: list[tuple[list[str], tuple[str, ...], str]] = []
        self.weak, self.sleep, self.cancelled = weak, sleep, False

    def check_usable(self) -> None:
        if self.weak:
            raise SandboxUnavailable("weak")

    async def run(self, argv: list[str], *, egress: Any, limits: Any, workload: str) -> Result:
        self.runs.append((argv, egress.hosts, workload))
        try:
            await asyncio.sleep(self.sleep)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return Result(exit_code=0, stdout="ok\n", stderr="")


async def test_shell_is_refused_without_a_strong_sandbox(world: dict[str, Path]) -> None:
    from nox.node.hands import ShellArgs

    for sandbox in (None, FakeSandbox(weak=True)):
        tools = _hands(world, sandbox)
        with pytest.raises(NodeRefusal) as caught:
            await tools["shell.run"].handler(ShellArgs(argv=["whoami"]))
        assert caught.value.reason == "sandbox_unavailable"
        if sandbox is not None:
            assert sandbox.runs == []  # nothing ran, also not "less confined"


async def test_shell_runs_in_the_sandbox(world: dict[str, Path]) -> None:
    from nox.node.hands import ShellArgs

    sandbox = FakeSandbox()
    tools = _hands(world, sandbox)
    out = await tools["shell.run"].handler(ShellArgs(command="df -h /"))
    assert out["ok"] and out["stdout"] == "ok\n"
    ((shell_argv, shell_egress, shell_work),) = sandbox.runs
    assert shell_argv[-1] == "df -h /" and shell_egress == () and shell_work == "shell"


def test_packages_are_not_offered_on_nodes_yet(world: dict[str, Path]) -> None:
    assert not {"package.install", "package.remove"} & (set(_hands(world)) | ROUTABLE)


def test_the_default_sandbox_never_accepts_the_weak_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import nox.sandbox
    from nox.node.hands import default_sandbox

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(nox.sandbox, "make_sandbox", lambda **kw: seen.append(kw) or "made")
    assert default_sandbox() == "made"
    assert seen == [{"accept_weak": False}]


def test_every_hand_is_offered_only_with_its_group(world: dict[str, Path]) -> None:
    tools = _hands(world)
    assert set(tools) <= ROUTABLE
    gate = NodeGate(tools, NodeManifest(), None, protected=world["node"])
    assert gate.offered() == []  # files and shell are off by default
    on = {g: g in ("files", "shell") for g in GROUPS}
    gate.set_manifest(NodeManifest(groups=on))
    assert {t.name for t in gate.offered()} == {"file.list", "file.read", "file.write", "shell.run"}


def test_folders_count_only_as_confirmed_on_the_device(tmp_path: Path) -> None:
    from nox.node.confirmed import manifest_key

    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    key = manifest_key(tmp_path, None)
    confirmed = NodeManifest(folders=[str(tmp_path / "a")])
    assert record_confirmed(audit, confirmed, key, create=True)
    edited = NodeManifest(folders=[str(tmp_path / "a"), str(tmp_path / "b")])
    assert effective_manifest(edited, audit, key).folders == [str(tmp_path / "a")]
    assert MOST_RESTRICTIVE.folders == []
    with pytest.raises(ValueError):
        NodeManifest(folders=["relative/path"])
    with pytest.raises(ValueError):
        NodeManifest(folders=["\\\\fileserver\\share"])


# ---- through the gate, the confirmation and the receipts (real listener) -------------------------


async def start(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    world: dict[str, Path],
    sandbox: Any,
    confirmer: Confirmer,
) -> tuple[NodeAgent, asyncio.Task[str], asyncio.Event, SqliteAuditLog]:
    node = await pair_node(pairing, tmp_path)
    home = NodeHome(world["node"])
    home.key_file.write_bytes(key_pem(node.key))
    home.save_pairing(
        NodeIdentity(
            node_id=node.node_id,
            name="Laptop",
            core_host="127.0.0.1",
            core_port=47843,
            node_port=listener.port,
            ca_fingerprint=tls.fingerprint.replace(":", "").lower(),
            paired_at=datetime.now(UTC),
        ),
        cert_pem=node.cert_pem,
        ca_pem=tls.ca_file.read_text(),
    )
    TARGET["core"] = tls.fingerprint.replace(":", "").lower()
    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    groups = {g: g in ("system", "files", "shell", "packages") for g in GROUPS}
    manifest = NodeManifest(groups=groups, folders=[str(world["root"])])
    holder: list[NodeGate] = []
    hands = NodeHands(
        folders=lambda: list(holder[0].manifest.folders),
        protected=(home.path,),
        sandbox=lambda: sandbox,
    )
    gate = NodeGate(hand_tools(hands), manifest, audit, protected=home.path)
    holder.append(gate)
    agent = NodeAgent(
        home,
        NodeKeyStore(home.key_file, None),
        gate,
        audit,
        ping_interval_s=3600,
        confirmer=confirmer,
    )
    stop = asyncio.Event()
    return agent, asyncio.create_task(agent.run(stop)), stop, audit


async def test_over_the_wire_every_hand_passes_gate_dialog_and_receipt(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    world: dict[str, Path],
) -> None:
    sandbox = FakeSandbox()
    confirmer = Confirmer("allow")
    agent, task, stop, audit = await start(
        listener, pairing, tls, tmp_path, world, sandbox, confirmer
    )
    signer = listener.signer  # type: ignore[attr-defined]
    try:
        session = await session_of(listener, agent._home.load_identity().node_id)  # type: ignore[union-attr]
        # A read needs no receipt and no dialog.
        listed = await call(
            session, signer, "file.read", {"path": "notes/einkauf.txt"}, risk="read"
        )
        assert "Haferflocken" in listed["content"][0]["text"]
        # The node folder by name: refused by the gate's text check before any handler.
        home_by_name = await call(
            session, signer, "file.read", {"path": str(world["node"] / "node.key")}, risk="read"
        )
        assert home_by_name["_nox"]["refused"] == "node_home_protected"
        # The node folder through a link: refused by the hand on the handle, and audited.
        link_folder(world["node"], world["root"] / "door")
        door = await call(session, signer, "file.read", {"path": "door/node.key"}, risk="read")
        assert door["_nox"]["refused"] in ("node_home_protected", "link_refused")
        # High risk: asked on this device, then run - in the sandbox.
        args = {"argv": ["whoami"], "egress": []}
        ran = await call(session, signer, "shell.run", args, risk="high", confirm_on_device=True)
        assert ran["structuredContent"]["ok"] is True and len(confirmer.asked) == 1
        # Without the core's receipt nothing with a side effect runs.
        other = {"argv": ["id"], "egress": []}
        meta = signed(signer, "shell.run", other, risk="high", device_id=session.device_id)
        params = {"name": "shell.run", "arguments": args, "_nox": meta}
        forged = await session.client.request("tools/call", params, timeout_s=10)
        assert forged["_nox"]["refused"] == "receipt_invalid"
        assert len(sandbox.runs) == 1
        actions = [e.action for e in audit.entries(since_seq=0, limit=200)]
        assert "node.refused" in actions
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_no_sandbox_on_the_device_means_refused_over_the_wire(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    world: dict[str, Path],
) -> None:
    agent, task, stop, _audit = await start(
        listener, pairing, tls, tmp_path, world, None, Confirmer("allow")
    )
    try:
        session = await session_of(listener, agent._home.load_identity().node_id)  # type: ignore[union-attr]
        answer = await call(
            session,
            listener.signer,  # type: ignore[attr-defined]
            "shell.run",
            {"argv": ["whoami"], "egress": []},
            risk="high",
            confirm_on_device=True,
        )
        assert answer["_nox"]["refused"] == "sandbox_unavailable"
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_a_kill_from_the_core_stops_the_sandboxed_run(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    world: dict[str, Path],
) -> None:
    sandbox = FakeSandbox(sleep=60)
    agent, task, stop, _audit = await start(
        listener, pairing, tls, tmp_path, world, sandbox, Confirmer("allow")
    )
    try:
        session = await session_of(listener, agent._home.load_identity().node_id)  # type: ignore[union-attr]
        running = asyncio.create_task(
            call(
                session,
                listener.signer,  # type: ignore[attr-defined]
                "shell.run",
                {"argv": ["sleep", "60"], "egress": []},
                risk="high",
                confirm_on_device=True,
            )
        )
        for _ in range(200):
            if sandbox.runs:
                break
            await asyncio.sleep(0.02)
        await listener.broadcast_kill("test")
        answer = await asyncio.wait_for(running, 5)
        assert answer["_nox"]["refused"] == "stopped"
        assert sandbox.cancelled is True
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


# ---- the core's side -----------------------------------------------------------------------------


def test_the_core_routes_the_hands_and_names_a_node_call_plainly() -> None:
    accepted = NodeRegistry._accept_tools(
        [{"name": n, "_nox": {"risk": "read"}} for n in ("shell.run", "file.write", "rm.rf")]
    )
    assert set(accepted) == {"shell.run", "file.write"}
    target = _shell_target({"argv": ["df", "-h"], "egress": ["deb.example.invalid"]})
    assert target == 'argv ["df", "-h"] egress ["deb.example.invalid"]'
    assert _shell_target({"command": "ls", "egress": []}) == 'command "ls" egress [none]'


def test_a_token_with_spaces_or_quotes_is_shown_as_one_token() -> None:
    # `["rm", "-rf /"]` and `["rm", "-rf", "/"]` must never look alike.
    assert _shell_target({"argv": ["rm", "-rf /"]}) != _shell_target({"argv": ["rm", "-rf", "/"]})
    assert '"say \\"hi\\""' in _shell_target({"argv": ["echo", 'say "hi"']})


def test_invisible_bidi_control_and_space_runs_are_made_visible() -> None:
    sneaky = _shell_target({"command": "echo safe\u202e\u200b\x1b[2J" + " " * 40 + "&& curl x"})
    assert "\u202e" not in sneaky and "\u200b" not in sneaky and "\x1b" not in sneaky
    assert "\\u202e" in sneaky and "\\u200b" in sneaky and "\\u001b" in sneaky
    assert "[40 spaces]" in sneaky and "&& curl x" in sneaky
    assert "\\n" in _shell_target({"command": "echo a\nrm -rf ~"})


def test_a_padded_call_is_refused_never_shortened() -> None:
    from nox.node.install import MAX_TARGET_CHARS
    from nox.node.router import CannotShow

    padded = {"command": "echo " + "a" * MAX_TARGET_CHARS + " ; rm -rf ~", "egress": []}
    with pytest.raises(CannotShow):
        _shell_target(padded)
    hosts = [f"h{i}.example.invalid" for i in range(8)]
    shown = _shell_target({"argv": ["curl", "x"], "egress": hosts})
    assert all(h in shown for h in hosts)  # the whole egress list, always


def _node_shell_spec(confirmer: bool = True) -> Any:
    from nox.node.hands import ShellArgs

    laptop = _entry("a" * 32, "Laptop", tools={"shell.run": "high"})
    laptop.confirmer = confirmer
    registry = FakeRegistry([laptop])
    spec = routed_spec(
        name="shell.run",
        description="t",
        risk=Risk.HIGH,
        side_effects=True,
        registry=registry,  # type: ignore[arg-type]
        input_model=ShellArgs,
        node_target=_shell_target,
    )
    return spec, registry


def test_a_node_shell_call_is_high_and_names_the_device() -> None:
    spec, _registry = _node_shell_spec()
    payload = spec.input_model.model_validate({"argv": ["whoami"], "device": "Laptop"}).model_dump()
    scope = spec.scope(payload)
    assert scope.risk is Risk.HIGH and scope.device == "a" * 32 and scope.confirm_on_device
    assert scope.target.endswith(': argv ["whoami"] egress [none]')


async def test_a_call_too_long_to_show_is_refused_before_anyone_is_asked() -> None:
    from nox.node.install import MAX_TARGET_CHARS

    spec, registry = _node_shell_spec(confirmer=False)
    command = "echo " + " x" * MAX_TARGET_CHARS + " ; curl evil.example.invalid"
    payload = spec.input_model.model_validate({"command": command, "device": "Laptop"}).model_dump()
    scope = spec.scope(payload)
    assert scope.risk is Risk.READ and scope.target == "device:none"  # nothing to confirm
    answer = await spec.handler(payload)
    assert answer["error"] == "cannot_show_in_full"
    assert registry.calls == []  # never sent to the node


# ---- allowing a folder: only on the device -------------------------------------------------------


def test_a_folder_is_allowed_only_in_a_terminal_here_and_never_the_node_folder(
    tmp_path: Path,
) -> None:
    from nox.node import cli as node_cli
    from nox.node.manifest import load_manifest

    home = NodeHome(tmp_path / "h")
    docs = tmp_path / "Dokumente"
    docs.mkdir()
    said: list[str] = []
    yes = {"interactive": lambda: True, "confirm": lambda _p: True}
    with pytest.raises(node_cli.NodeCliError):  # not from a non-interactive caller (the core)
        node_cli.allow(home, "files", True, say=said.append, folder=str(docs))
    node_cli.allow(home, "files", True, say=said.append, folder=str(docs), **yes)  # type: ignore[arg-type]
    assert load_manifest(home.manifest_file).folders == [os.path.realpath(docs)]
    for bad in (str(home.path), str(home.path / "sub"), "relative", r"\\fileserver\share"):
        with pytest.raises(node_cli.NodeCliError):
            node_cli.allow(home, "files", True, say=said.append, folder=bad, **yes)  # type: ignore[arg-type]
    with pytest.raises(node_cli.NodeCliError):
        node_cli.allow(home, "shell", True, say=said.append, folder=str(docs), **yes)  # type: ignore[arg-type]
    node_cli.allow(home, "files", False, say=said.append, folder=str(docs))
    assert load_manifest(home.manifest_file).folders == []


# ---- review round 1: what a write may never do ---------------------------------------------------

REFUSED_WRITES = ("execution_trigger", "hidden_refused", "extension_refused", "config_refused")


@pytest.mark.parametrize(
    "target",
    [
        ".git/hooks/pre-commit",
        "repo/.GIT/config",
        "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/hello.txt",
        ".bashrc",
        ".zprofile",
        ".config/autostart/nox.desktop",
        ".config/systemd/user/nox.service",
        "Library/LaunchAgents/example.invalid.plist",
        "Documents/WindowsPowerShell/profile.txt",
        "notes/run.ps1",
        "notes/RUN.CMD",
        "notes/start.sh",
        "notes/link.lnk",
        "notes/tool.exe",
    ],
)
async def test_a_write_never_plants_an_execution_trigger(
    world: dict[str, Path], target: str
) -> None:
    folder = (world["root"] / target).parent
    folder.mkdir(parents=True, exist_ok=True)
    with pytest.raises(NodeRefusal) as caught:
        await _hands(world)["file.write"].handler(WriteArgs(path=target, text="echo hi"))
    assert caught.value.reason in REFUSED_WRITES
    assert not (world["root"] / target).exists()


async def test_a_link_into_a_git_folder_is_no_way_around(world: dict[str, Path]) -> None:
    hooks = world["root"] / "repo" / ".git" / "hooks"
    hooks.mkdir(parents=True)
    link_folder(hooks, world["root"] / "innocent")
    with pytest.raises(NodeRefusal) as caught:
        await _hands(world)["file.write"].handler(WriteArgs(path="innocent/pre-commit", text="x"))
    assert caught.value.reason in (*REFUSED_WRITES, "link_refused")
    assert not (hooks / "pre-commit").exists()


@pytest.mark.skipif(WINDOWS, reason="the executable bit")
async def test_an_executable_is_never_written_and_a_new_file_never_executable(
    world: dict[str, Path],
) -> None:
    script = world["root"] / "notes" / "tool.txt"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    with pytest.raises(NodeRefusal) as caught:
        await _hands(world)["file.write"].handler(
            WriteArgs(path="notes/tool.txt", text="x", overwrite=True)
        )
    assert caught.value.reason == "executable_refused"
    await _hands(world)["file.write"].handler(WriteArgs(path="notes/neu.txt", text="x"))
    assert (world["root"] / "notes" / "neu.txt").stat().st_mode & 0o111 == 0


async def test_a_file_with_a_second_name_is_not_written(world: dict[str, Path]) -> None:
    os.link(world["root"] / "notes" / "einkauf.txt", world["root"] / "zweit.txt")
    with pytest.raises(NodeRefusal) as caught:
        await _hands(world)["file.write"].handler(
            WriteArgs(path="zweit.txt", text="", overwrite=True)
        )
    assert caught.value.reason == "hard_link_refused"
    assert (world["root"] / "notes" / "einkauf.txt").read_bytes() == b"Haferflocken\n"


def test_broad_folders_are_never_allowed(world: dict[str, Path], tmp_path: Path) -> None:
    from nox.node import cli as node_cli
    from nox.node.safe_fs import broad_folder

    system = [os.environ["SystemRoot"], os.environ["ProgramFiles"]] if WINDOWS else ["/etc", "/usr"]
    for place in [Path.home().anchor, str(Path.home()), str(Path.home().parent), *system]:
        assert broad_folder(place), place
        assert Folders.build([place], [world["node"]]).roots == ()
    assert not broad_folder(str(world["root"]))
    yes = {"interactive": lambda: True, "confirm": lambda _p: True}
    with pytest.raises(node_cli.NodeCliError):
        node_cli.allow(  # type: ignore[arg-type]
            NodeHome(tmp_path / "h"),
            "files",
            True,
            say=lambda _t: None,
            folder=str(Path.home()),
            **yes,
        )


def test_a_link_into_a_secret_store_is_refused(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = world["root"].parent / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    link_folder(home / ".ssh", world["root"] / "keys")
    hands = NodeHands(
        folders=lambda: [str(world["root"])], protected=(world["node"],), sandbox=lambda: None
    )
    assert refused(hands.resolved(), "keys/id_ed25519") in ("node_home_protected", "link_refused")


async def test_replacing_or_extending_a_file_is_high_and_shown_with_size_and_preview(
    tmp_path: Path,
) -> None:
    from nox.node.agent import confirmation_text
    from nox.node.receipts import ReceiptSigner, ReceiptVerifier

    signer = ReceiptSigner(tmp_path / "r")
    verifier = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    groups = {g: g == "files" for g in GROUPS}
    world_tools = hand_tools(
        NodeHands(folders=lambda: [], protected=(tmp_path / "n",), sandbox=lambda: None)
    )
    gate = NodeGate(world_tools, NodeManifest(groups=groups), None, receipts=verifier)
    args = {"path": "a.txt", "text": "x" * 500, "append": False, "overwrite": True}
    low = signed(signer, "file.write", args, risk="medium", device_id="d" * 32, core_id="c" * 64)
    assert gate.check("file.write", args, meta=NoxCallMeta(**low)).reason == "risk_understated"
    high = signed(signer, "file.write", args, risk="high", device_id="d" * 32, core_id="c" * 64)
    decision = gate.check("file.write", args, meta=NoxCallMeta(**high))
    assert decision.allowed and decision.needs_local_confirmation
    tool = world_tools["file.write"]
    assert tool.shown is not None and decision.args is not None
    text = confirmation_text("file.write", tool.shown(decision.args))
    assert text is not None and '"overwrite"' in text and "500" in text
    assert "content shortened - 100 characters not shown" in text


async def test_a_node_write_is_high_when_it_replaces_and_never_remembered() -> None:
    from nox.node.install import _write_risk, _write_target
    from nox.security.model import Decision, PermissionResult
    from nox.tools.executor import ToolExecutor
    from nox.tools.registry import ToolRegistry

    laptop = _entry("a" * 32, "Laptop", tools={"file.write": "medium"})
    laptop.confirmer = True
    registry = FakeRegistry([laptop])
    spec = routed_spec(
        name="file.write",
        description="t",
        risk=Risk.MEDIUM,
        side_effects=True,
        registry=registry,  # type: ignore[arg-type]
        input_model=WriteArgs,
        node_target=_write_target,
        node_risk=_write_risk,
        remember_on_node=False,
    )
    payload = {"path": "a.txt", "text": "neu", "device": "Laptop"}
    new = spec.scope(spec.input_model.model_validate(payload).model_dump())  # type: ignore[misc]
    assert new.risk is Risk.MEDIUM and not new.rememberable
    assert '"new file"' in new.target and "bytes 3" in new.target
    over = {**payload, "overwrite": True}
    replaced = spec.scope(spec.input_model.model_validate(over).model_dump())  # type: ignore[misc]
    assert replaced.risk is Risk.HIGH and replaced.confirm_on_device and not replaced.rememberable

    seen: list[Any] = []

    class Engine:
        def check(self, request: Any) -> PermissionResult:
            seen.append(request)
            return PermissionResult(decision=Decision.DENY, rule_id="test")

        def is_tainted(self, conversation: str) -> bool:
            return False

    class Audit:
        def append(self, **_k: Any) -> int:
            return 0

    class Kill:
        def is_engaged(self) -> bool:
            return False

    tools = ToolRegistry()
    tools.register(spec)
    executor = ToolExecutor(tools, Engine(), Audit(), None, Kill())  # type: ignore[arg-type]
    await executor.call("companion", "file.write", payload, mode="companion")
    assert seen and seen[0].rememberable is False  # a "remember" answer creates no grant


def test_the_device_dialog_and_the_core_target_render_alike() -> None:
    from nox.node.agent import confirmation_text
    from nox.node.install import _write_target
    from nox.node.protocol import render

    argv = ["echo", "safe‮", "a" + " " * 9 + "b", "Ж"]
    dialog = confirmation_text("shell.run", {"argv": argv})
    assert dialog is not None and render(argv) in dialog
    assert render(argv) in _shell_target({"argv": argv})
    assert "‮" not in dialog and "Ж" not in dialog and "[9 spaces]" in dialog
    shown = {"path": "a.txt", "text": "Grüße", "overwrite": True}
    assert render("Grüße") in _write_target(shown) and render("Grüße") == '"Grüße"'


def test_free_text_is_not_a_path_but_a_path_still_is(tmp_path: Path) -> None:
    from nox.node.hands import ShellArgs as _Shell  # noqa: F401 - the model the gate validates

    gate = NodeGate(
        hand_tools(NodeHands(folders=lambda: [], protected=(tmp_path,), sandbox=lambda: None)),
        NodeManifest(groups={g: g in ("files", "shell") for g in GROUPS}),
        None,
    )
    for args in ({"argv": ["git", "log", "HEAD~1"], "egress": []}, {"command": "cd ~/x && ls"}):
        assert gate.check("shell.run", args).reason != "path_alias_refused"
    assert gate.check("file.read", {"path": "../x"}).reason == "path_alias_refused"
    assert gate.check("file.read", {"path": "~/x"}).reason == "path_alias_refused"


@pytest.mark.skipif(WINDOWS, reason="FIFOs")
def test_a_fifo_is_refused_without_blocking(world: dict[str, Path]) -> None:
    os.mkfifo(world["root"] / "pipe")
    assert refused(folders(world), "pipe") == "not_a_file"


@pytest.mark.skipif(not WINDOWS, reason="the Windows listing")
def test_a_windows_listing_whose_folder_changed_is_refused(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import safe_fs

    monkeypatch.setattr(safe_fs, "_win_final", lambda _fd: str(world["outside"]))
    with pytest.raises(OutsideFolders) as caught:
        with list_folder(folders(world), "notes"):
            pass
    assert caught.value.reason == "changed"


async def test_a_huge_listing_stops_counting(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import hands as hands_module

    monkeypatch.setattr(hands_module, "MAX_SCAN", 3)
    for i in range(6):
        (world["root"] / "notes" / f"f{i}.txt").write_text("x", encoding="utf-8")
    listing = await _hands(world)["file.list"].handler(PathArgs(path="notes"))
    assert listing["total"] == 3 and listing["stopped_counting"] and listing["truncated"]


def test_utf8_cut_by_the_limit_stays_utf8() -> None:
    from nox.node.hands import _decode

    cut = ("aaa" + "é").encode("utf-8")[:-1]
    assert _decode(cut, True) == ("aaa", "utf-8")
    assert _decode(b"caf\xe9 au lait", False)[1] == "cp1252"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bwrap's read-only binds")
async def test_a_node_folder_under_a_system_bind_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.sandbox import linux

    monkeypatch.setattr(linux, "visible_system_paths", lambda: (str(tmp_path),))
    hands = NodeHands(
        folders=lambda: [], protected=(tmp_path / "node",), sandbox=lambda: FakeSandbox()
    )
    assert hands.exposed() is not None
    with pytest.raises(NodeRefusal) as caught:
        await hands.confined()
    assert caught.value.reason == "sandbox_unavailable"


# ---- review round 2: writes are an allowlist of data files ------------------------------------


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (".gitconfig.txt", "hidden_refused"),
        ("notes/.vscode/notes.md", "hidden_refused"),
        ("notes/.hidden/a.txt", "hidden_refused"),
        ("notes/Makefile", "extension_refused"),
        ("notes/README", "extension_refused"),
        ("notes/page.svg", "extension_refused"),
        ("notes/archive.zip", "extension_refused"),
        ("notes/config.ini", "extension_refused"),
        ("notes/pip.conf", "extension_refused"),
        ("notes/settings.json", "config_refused"),
        ("notes/TASKS.JSON", "config_refused"),
        ("notes/package.json", "config_refused"),
        ("notes/docker-compose.yml", "config_refused"),
        ("notes/appsettings.Development.json", "config_refused"),
        ("notes/workflows/build.yml", "config_refused"),
    ],
)
async def test_only_plain_data_files_are_written(
    world: dict[str, Path], target: str, reason: str
) -> None:
    (world["root"] / target).parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(NodeRefusal) as caught:
        await _hands(world)["file.write"].handler(WriteArgs(path=target, text="x"))
    assert caught.value.reason == reason
    assert not (world["root"] / target).exists()


@pytest.mark.parametrize("target", ["notes/liste.TXT", "notes/plan.md", "notes/daten.csv"])
async def test_data_files_are_written(world: dict[str, Path], target: str) -> None:
    answer = await _hands(world)["file.write"].handler(WriteArgs(path=target, text="x"))
    assert answer["ok"] is True


async def test_a_link_cannot_turn_a_data_name_into_a_hidden_folder(world: dict[str, Path]) -> None:
    hidden = world["root"] / "notes" / ".config"
    hidden.mkdir()
    link_folder(hidden, world["root"] / "visible")
    with pytest.raises(NodeRefusal) as caught:
        await _hands(world)["file.write"].handler(WriteArgs(path="visible/a.txt", text="x"))
    assert caught.value.reason in ("hidden_refused", "link_refused")
    assert not (hidden / "a.txt").exists()


def test_a_long_write_shows_head_tail_hash_and_what_is_not_shown() -> None:
    import hashlib

    from nox.node.hands import PREVIEW_CHARS, write_summary

    text = "A" * PREVIEW_CHARS + "M" * 50 + "\n" + "Z" * PREVIEW_CHARS
    shown = write_summary("a.txt", text, append=False, overwrite=True)
    assert shown["content_start"] == "A" * PREVIEW_CHARS
    assert shown["content_end"] == "Z" * PREVIEW_CHARS
    assert shown["not_shown"] == "content shortened - 51 characters not shown"
    assert shown["sha256"] == hashlib.sha256(text.encode()).hexdigest()[:16]
    assert shown["lines"] == 2 and "content" not in shown
    short = write_summary("a.txt", "kurz\n", append=False, overwrite=False)
    assert short["content"] == "kurz\n" and "not_shown" not in short


def test_the_write_is_rendered_alike_on_the_core_and_on_the_device() -> None:
    from nox.node.agent import confirmation_text
    from nox.node.hands import PREVIEW_CHARS, write_summary
    from nox.node.install import _write_target
    from nox.node.protocol import render

    text = "Anfang " + "x" * (2 * PREVIEW_CHARS) + " Ende \u202e"
    payload = {"path": "notes/a.txt", "text": text, "append": True, "overwrite": False}
    shown = write_summary(payload["path"], text, append=True, overwrite=False)
    target = _write_target(payload)
    dialog = confirmation_text("file.write", shown)
    assert dialog is not None
    for key, value in shown.items():
        assert f"{key} {render(value)}" in target
        assert f"{key}: {render(value)}" in dialog
    assert "\u202e" not in dialog and "\u202e" not in target
