"""Shared helpers of the node tests: a paired node running against the real listener over
real TLS, receipts signed the way the core signs them, a call sent exactly as a test
wants it, and small fakes for the registry and a node's tool listing.

Test modules import from here (and from the other `support_*` modules), never from each
other. Fixtures live in `conftest.py`."""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import sqlite3
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import BaseModel, ConfigDict
from websockets.asyncio.client import ClientConnection, connect

from nox.lan.tls import TlsMaterial
from nox.node.agent import NodeAgent
from nox.node.gate import NodeGate
from nox.node.home import NodeHome, NodeIdentity
from nox.node.keystore import NodeKeyStore
from nox.node.listener import NodeListener, NodeSession
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.pairing import NodePairing
from nox.node.protocol import NODE_PATH, NodePairRequest, NoxCallMeta
from nox.node.receipts import ReceiptSigner, ReceiptVerifier
from nox.node.registry import NodeEntry
from nox.node.tools import NodeTool, default_tools
from nox.security.audit import SqliteAuditLog

NAMES = ("localhost", "wohnzimmer-pi.local")


ADDRESSES = ("127.0.0.1", "192.168.1.20")


MANIFEST = "0" * 64


def make_key() -> ec.EllipticCurvePrivateKey:
    """A fresh P-256 key, as a node generates one before pairing."""
    return ec.generate_private_key(ec.SECP256R1())


def make_csr(key: Any, *, cn: str = "Laptop", extra: bool = False) -> str:
    """A certificate request for `key`; `extra` adds an extension the authority must refuse."""
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    )
    if extra:  # what a greedy node might ask for: a CA, a server name
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        ).add_extension(x509.SubjectAlternativeName([x509.DNSName("evil.example")]), critical=False)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


def key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    """The key as unencrypted PKCS#8 PEM, the way a node without a keyring stores it."""
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@dataclass
class PairedNode:
    """What pairing leaves on a node: its id, key and certificate (also as files)."""

    node_id: str
    key: ec.EllipticCurvePrivateKey
    cert_pem: str
    serial: str
    cert_file: Path
    key_file: Path


async def pair_node(pairing: NodePairing, tmp_path: Path, name: str = "") -> PairedNode:
    """Pair a node the way `nox node pair` does and return what it keeps."""
    key = make_key()
    start = await pairing.start(name=name)
    redeemed = await pairing.redeem(
        NodePairRequest(
            code=start.code,
            csr=make_csr(key),
            name=name,
            os="linux",
            arch="x86_64",
            version="0.7.0",
        )
    )
    assert redeemed.result.ok and redeemed.issued is not None, redeemed.result
    folder = tmp_path / f"node-{redeemed.result.device_id[:8]}"
    folder.mkdir()
    (folder / "node.crt").write_text(redeemed.issued.cert_pem)
    (folder / "node.key").write_bytes(key_pem(key))
    return PairedNode(
        node_id=redeemed.result.device_id,
        key=key,
        cert_pem=redeemed.issued.cert_pem,
        serial=redeemed.issued.serial,
        cert_file=folder / "node.crt",
        key_file=folder / "node.key",
    )


def client_context(
    tls: TlsMaterial, node: PairedNode | None = None, *, max_tls12: bool = False
) -> ssl.SSLContext:
    """A TLS client context that trusts this core and, with `node`, presents its certificate."""
    context = ssl.create_default_context(cafile=str(tls.ca_file))
    if node is not None:
        context.load_cert_chain(str(node.cert_file), str(node.key_file))
    if max_tls12:
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


async def open_node(
    listener: NodeListener,
    context: ssl.SSLContext,
    *,
    origin: str | None = None,
    path: str = NODE_PATH,
) -> ClientConnection:
    """Open the WebSocket to the node listener on loopback; no hello is sent yet."""
    return await connect(
        f"wss://127.0.0.1:{listener.port}{path}",
        ssl=context,
        server_hostname="localhost",
        origin=origin,  # type: ignore[arg-type]
        open_timeout=5,
        ping_interval=None,
        compression=None,
        max_size=2 * 1024 * 1024,
    )


def hello(node_protocol: int = 1) -> dict[str, Any]:
    """The `initialize` request a well-behaved node sends first."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "node_protocol": node_protocol,
            "node_version": "0.7.0",
            "os": "linux",
            "arch": "x86_64",
            "manifest_hash": MANIFEST,
        },
    }


async def recv_json(ws: ClientConnection, timeout: float = 5.0) -> dict[str, Any]:
    """The next message on the socket, parsed."""
    message: dict[str, Any] = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    return message


async def recv_until(ws: ClientConnection, predicate: Any, timeout: float = 5.0) -> dict[str, Any]:
    """Read messages until one matches `predicate`; that one is returned."""

    async def loop() -> dict[str, Any]:
        while True:
            message = await recv_json(ws, timeout)
            if predicate(message):
                return message

    return await asyncio.wait_for(loop(), timeout)


async def greet(ws: ClientConnection, node_protocol: int = 1) -> dict[str, Any]:
    """Send the hello and return the listener's answer to it."""
    await ws.send(json.dumps(hello(node_protocol)))
    return await recv_until(ws, lambda m: m.get("id") == 1)


_CALL_IDS = itertools.count()


class TargetArgs(BaseModel):
    """The arguments of the scripted tools: one free string."""

    model_config = ConfigDict(extra="forbid")
    target: str = "x"


class Confirmer:
    """A scripted dialog on the device: it answers `answer` and records what it was asked."""

    def __init__(self, answer: str = "allow", *, available: bool = True) -> None:
        self.answer, self._available, self.asked = answer, available, []  # type: ignore[var-annotated]
        #: A scripted device without a dialog stands for one without a screen (only that one
        #: may trust the core for high risk).
        self.screen = available

    @property
    def available(self) -> bool:
        return self._available

    async def ask(self, text: str, *, timeout_s: int = 60) -> str:
        self.asked.append(text)
        return self.answer


@dataclass
class Node:
    """A node started by `start_node`: its agent, folder, running task and what its tools ran."""

    agent: NodeAgent
    home: NodeHome
    node_id: str
    task: asyncio.Task[str]
    stop: asyncio.Event
    ran: list[str]
    audit: SqliteAuditLog


def scripted_tools(ran: list[str]) -> dict[str, NodeTool]:
    """The default tools plus `system.wait` (never returns), `system.high` and `system.low`;
    each appends to `ran` when it really runs."""

    async def wait(_a: BaseModel) -> dict[str, Any]:
        ran.append("wait-start")
        await asyncio.sleep(60)
        ran.append("wait-end")
        return {}

    async def high(_a: BaseModel) -> dict[str, Any]:
        ran.append("high")
        return {"done": True}

    async def low(_a: BaseModel) -> dict[str, Any]:
        ran.append("low")
        return {"done": True}

    tools = default_tools(None)
    tools["system.wait"] = NodeTool(
        "system.wait", "system", "low", "t", TargetArgs, wait, path_safe=True
    )
    tools["system.high"] = NodeTool(
        "system.high", "system", "high", "t", TargetArgs, high, path_safe=True
    )
    tools["system.low"] = NodeTool(
        "system.low", "system", "low", "t", TargetArgs, low, path_safe=True
    )
    return tools


async def start_node(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    *,
    confirmer: Confirmer | None = None,
    manifest: NodeManifest | None = None,
    receipt_key: str = "",
) -> Node:
    """Pair a node, write its folder and run its agent against `listener` over real TLS.

    The receipt target of the running test is pointed at this core."""
    node = await pair_node(pairing, tmp_path)
    home = NodeHome(tmp_path / f"home-{node.node_id[:6]}")
    home.ensure()
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
            receipt_key=receipt_key,
        ),
        cert_pem=node.cert_pem,
        ca_pem=tls.ca_file.read_text(),
    )
    current_receipt_target()["core"] = tls.fingerprint.replace(":", "").lower()
    ran: list[str] = []
    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    gate = NodeGate(scripted_tools(ran), manifest or NodeManifest(), audit, protected=home.path)
    agent = NodeAgent(
        home,
        NodeKeyStore(home.key_file, None),
        gate,
        audit,
        ping_interval_s=3600,
        confirmer=confirmer or Confirmer(),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run(stop))
    return Node(agent, home, node.node_id, task, stop, ran, audit)


async def session_of(listener: NodeListener, node_id: str) -> NodeSession:
    """The listener's session of `node_id`, once the node is connected and ready."""
    for _ in range(300):
        session = listener.sessions.get(node_id)
        if session is not None and session.ready.is_set():
            return session
        await asyncio.sleep(0.02)
    raise AssertionError("the node never connected")


#: The node and core the receipts of the running test are for. The `receipt_target`
#: fixture (conftest, autouse) installs a fresh one for every test, so a test that starts
#: a node - which points it at that test's core - leaves nothing behind for the next.
_receipt_target: dict[str, str] | None = None


def current_receipt_target() -> dict[str, str]:
    """The receipt target of the running test (see the `receipt_target` fixture)."""
    if _receipt_target is None:
        raise AssertionError("no receipt target: the `receipt_target` fixture is not active")
    return _receipt_target


def signed(
    signer: ReceiptSigner,
    name: str,
    arguments: Any,
    *,
    risk: str = "low",
    confirm_on_device: bool = False,
    deadline: float | None = None,
    issued_at: float | None = None,
    call_id: str | None = None,
    decided_by: str = "user",
    tainted: bool = False,
    device_id: str | None = None,
    core_id: str | None = None,
) -> dict[str, Any]:
    """The `_nox` metadata of a call with a receipt signed by `signer` - exactly what the test
    says, also when a real core would never say it."""
    call_id = call_id or f"k{next(_CALL_IDS)}"
    issued_at = time.time() if issued_at is None else issued_at
    deadline = issued_at + 30 if deadline is None else deadline
    device_id = device_id or current_receipt_target()["device"]
    core_id = core_id or current_receipt_target()["core"]
    return {
        "call_id": call_id,
        "risk": risk,
        "deadline": deadline,
        "issued_at": issued_at,
        "decided_by": decided_by,
        "confirm_on_device": confirm_on_device,
        "tainted": tainted,
        "receipt": signer.sign(
            call_id=call_id,
            device_id=device_id,
            core_id=core_id,
            tool=name,
            arguments=arguments,
            risk=risk,
            decided_by=decided_by,
            confirm_on_device=confirm_on_device,
            tainted=tainted,
            issued_at=issued_at,
            deadline=deadline,
        ),
    }


async def call(
    session: NodeSession,
    signer: ReceiptSigner,
    name: str,
    arguments: Any | None = None,
    *,
    timeout: float = 10,
    **meta: Any,
) -> dict[str, Any]:
    """Send one signed `tools/call` over the session and return the node's answer."""
    args = {} if arguments is None else arguments
    meta.setdefault("device_id", session.device_id)
    params = {"name": name, "arguments": args, "_nox": signed(signer, name, args, **meta)}
    return await session.client.request("tools/call", params, timeout_s=timeout)


def percentile(values: list[float], pct: float) -> float:
    """The nearest-rank percentile of `values` (p99 of 20 rounds is the slowest one)."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def kill_switch_for(listener: NodeListener) -> tuple[Any, list[float], list[dict[str, bool]]]:
    """A `KillSwitchService` whose "nodes" stop hook is the listener's `broadcast_kill` (and its
    resume hook `broadcast_resume`), wired as `nox.node.install` wires them. Also returns when
    each kill's last `nox/killed` came in (`broadcast_kill` returns once every node answered or
    its wait ran out) and which nodes answered."""
    from nox.security.killswitch import KillSwitchService

    class Audit:
        def append(self, **_k: Any) -> int:
            return 0

    switch = KillSwitchService(audit=Audit())  # type: ignore[arg-type]
    all_killed: list[float] = []
    acks: list[dict[str, bool]] = []

    async def stop_nodes() -> None:
        acks.append(await listener.broadcast_kill("kill switch"))
        all_killed.append(time.monotonic())

    async def resume_nodes() -> None:
        await listener.broadcast_resume()

    switch.register_stop_hook("nodes", stop_nodes)
    switch.register_resume_hook("nodes", resume_nodes)
    return switch, all_killed, acks


DEVICE_ID = "d" * 32


CORE_ID = "c" * 64


def gate_with_every_group(
    tmp_path: Path, manifest: NodeManifest | None = None, audit: Any = None
) -> tuple[NodeGate, ReceiptSigner]:
    """A gate with every group switched on, a text tool and a low tool, and the signer whose
    receipts it accepts (for `DEVICE_ID` and `CORE_ID`)."""
    signer = ReceiptSigner(tmp_path / "r")
    home = tmp_path / "node-home"
    home.mkdir(exist_ok=True)

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str = "x"
        path: str = ""

    async def run(_a: BaseModel) -> dict[str, Any]:
        return {}

    tools = default_tools(None)
    tools["desktop.type_text"] = NodeTool(
        "desktop.type_text", "desktop", "low", "t", Args, run, path_safe=True
    )
    tools["system.low"] = NodeTool("system.low", "system", "low", "t", Args, run, path_safe=True)
    gate = NodeGate(
        tools,
        manifest or NodeManifest(groups={g: True for g in GROUPS}),
        audit,
        protected=home,
    )
    gate.receipts = ReceiptVerifier(signer.public_key, device_id=DEVICE_ID, core_id=CORE_ID)
    return gate, signer


def call_meta(signer: ReceiptSigner, name: str, args: Any, **kw: Any) -> NoxCallMeta:
    """Validated call metadata with a receipt for `DEVICE_ID` and `CORE_ID`."""
    return NoxCallMeta.model_validate(
        signed(signer, name, args, device_id=DEVICE_ID, core_id=CORE_ID, **kw)
    )


# ---- M5: what this device allows is decided by a person at this device ---------------------------


def node_home(tmp_path: Path, *, paired: bool = True) -> Any:
    """A node folder; `paired` records the defaults a pairing confirms on the device."""
    from nox.node import cli as node_cli
    from nox.node.home import NodeHome

    home = NodeHome(tmp_path / "node-home")
    home.ensure()
    if paired:
        node_cli.confirm_defaults(home, None)
    return home


def registry_entry(
    device_id: str,
    name: str,
    *,
    online: bool = True,
    tools: dict[str, str] | None = None,
    compatible: bool = True,
    linked: str | None = None,
) -> NodeEntry:
    """A registry entry for a device with the given tools (name -> risk) and state."""
    return NodeEntry(
        device_id=device_id,
        name=name,
        online=online,
        compatible=compatible,
        tools={"notify.show": "low", "system.info": "read"} if tools is None else tools,
        last_seen=datetime(2026, 10, 21, 14, 2, tzinfo=UTC),
        linked_device_id=linked,
    )


# ---- the routed spec ------------------------------------------------------------------------


class FakeRegistry:
    """A registry with fixed entries that records every call routed to a node."""

    def __init__(self, entries: list[NodeEntry]) -> None:
        self._entries = entries
        self.calls: list[tuple[str, str, dict[str, Any], str]] = []

    def entries(self) -> list[NodeEntry]:
        return self._entries

    def known(self) -> list[NodeEntry]:
        return self._entries

    async def settle(self, device_ids: Any = None, timeout_s: float = 3.0) -> None:
        """No device is still connecting here (the router waits for that before a call)."""
        return None

    async def call(
        self,
        entry: NodeEntry,
        tool: str,
        arguments: dict[str, Any],
        *,
        risk: str,
        timeout_s: float,
        confirm_on_device: bool = False,
    ) -> dict[str, Any]:
        if not entry.online:
            return {"error": "device_offline", "device": entry.name}
        self.calls.append((entry.device_id, tool, arguments, risk))
        return {"device": entry.name, "result": {"shown": True}}


class ListingClient:
    """A node's MCP client: answers `tools/list` with a fixed list, records what it was sent."""

    def __init__(self, tools: list[dict[str, Any]]) -> None:
        self.tools = tools
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def request(self, method: str, params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.sent.append((method, params))
        if method == "tools/list":
            return {"tools": self.tools}
        return {}


class ListingSession:
    """A listener session around a `ListingClient`, as the registry sees it."""

    def __init__(self, device: Any, client: ListingClient) -> None:
        self.device = device
        self.client = client
        self.compatible = True
        self.alive = True

        class Hello:
            confirmer = False

        self.hello = Hello()

    @property
    def device_id(self) -> str:
        return str(self.device.id)


class SlowListingClient(ListingClient):
    """A node whose first `tools/list` answer takes a moment to arrive."""

    def __init__(self, tools: list[dict[str, Any]]) -> None:
        super().__init__(tools)
        self.answer = asyncio.Event()

    async def request(self, method: str, params: dict[str, Any], **kw: Any) -> dict[str, Any]:
        if method == "tools/list":
            await self.answer.wait()
        return await super().request(method, params, **kw)
