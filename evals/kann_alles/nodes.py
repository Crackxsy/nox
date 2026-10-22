"""A simulated node for the benchmark world: "Wohnzimmer-Pi", a headless Raspberry Pi.

The core under test routes to it exactly as to a real node - the router, the device-scoped
permission request, a signed receipt per call - and on the "Pi" every call passes the real
`NodeGate` (capability groups, risk ceiling, receipt, budget) and the real node hands. Only two
things are fakes, so nothing ever runs on the machine the benchmark runs on:

* the transport: calls are handed to the node in-process instead of over mTLS;
* the sandbox: `FakePiSandbox` answers a few well-known commands (`df`, the temperature,
  `systemctl`) with fixed text and refuses everything else. Updates, reboots and installs never
  get here in a benchmark anyway - they are high risk, and the harness denies their dialog.

The Pi is headless, so high risk is confirmed on the core (one dialog the harness answers) and the
node accepts that only because its manifest says `trust_core_for_high` - as a real headless node
would after `nox node allow trust_core_for_high` on the device.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nox.node.gate import NodeGate
from nox.node.hands import NodeHands, hand_tools
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.protocol import NoxCallMeta
from nox.node.receipts import ReceiptSigner, ReceiptVerifier, normalize_arguments
from nox.node.registry import NodeEntry, NodeRegistry
from nox.node.tools import NoArgs, NodeRefusal, NodeTool
from nox.sandbox import Result, SandboxMode

__all__ = ["PI_ID", "PI_NAME", "FakePiSandbox", "WorldNodes"]

PI_NAME = "Wohnzimmer-Pi"
PI_ID = "5e1f" * 8
CORE_ID = "c0de" * 16

_DF = (
    "Filesystem      Size  Used Avail Use% Mounted on\n"
    "/dev/mmcblk0p2   29G   12G   16G  43% /\n"
    "/dev/mmcblk0p1  510M   64M  447M  13% /boot/firmware\n"
)
_STATUS = (
    "● nox.service - Nox\n"
    "     Loaded: loaded (/etc/systemd/system/nox.service; enabled)\n"
    "     Active: active (running) since Tue 2026-10-20 07:12:03 CEST; 1 day 3h ago\n"
)


class FakePiSandbox:
    """Stands in for the Pi's bwrap: fixed answers, nothing is ever executed."""

    mode = SandboxMode.LINUX_BWRAP

    def __init__(self) -> None:
        self.runs: list[list[str]] = []

    def check_usable(self) -> None:
        return None

    async def run(self, argv: list[str], **_kwargs: Any) -> Result:
        self.runs.append(list(argv))
        line = " ".join(argv[-1:] if len(argv) > 2 and argv[1] == "-c" else argv).lower()
        if "measure_temp" in line:
            return Result(0, "temp=52.1'C\n", "")
        if "thermal_zone" in line:
            return Result(0, "52100\n", "")
        if line.split()[:1] and line.split()[0].endswith("df"):
            return Result(0, _DF, "")
        if "systemctl" in line and "is-active" in line:
            return Result(0, "active\n", "")
        if "systemctl" in line and "status" in line:
            return Result(0, _STATUS, "")
        return Result(127, "", "not available on the simulated Pi\n")


async def _pi_info(_args: Any) -> dict[str, Any]:
    return {
        "os": "Linux",
        "os_version": "6.6.51-v8+",
        "arch": "aarch64",
        "cpu_count": 4,
        "cpu_percent": 7.5,
        "memory_total_mb": 3794,
        "memory_used_percent": 31.0,
        "disk_total_gb": 29.0,
        "disk_free_gb": 16.2,
        "uptime_s": 98_400,
        "temperature_c": 52.1,
    }


@dataclass
class _Client:
    node: SimulatedNode
    closed: bool = False

    async def request(self, method: str, params: dict[str, Any], *, timeout_s: float) -> Any:
        del timeout_s
        if method != "tools/call":
            return {}
        return await self.node.call(params)


@dataclass
class _Session:
    client: _Client
    device_id: str = PI_ID
    alive: bool = True


@dataclass
class SimulatedNode:
    """The node side of the Pi: the real gate and hands with the fake sandbox and system values."""

    verifier: ReceiptVerifier
    sandbox: FakePiSandbox = field(default_factory=FakePiSandbox)
    gate: NodeGate = field(init=False)

    def __post_init__(self) -> None:
        sandbox = self.sandbox
        hands = NodeHands(folders=lambda: [], protected=(), sandbox=lambda: sandbox)  # type: ignore[arg-type, return-value]
        tools: dict[str, NodeTool] = hand_tools(hands)
        tools["system.info"] = NodeTool(
            "system.info", "system", "read", "System values of the Pi.", NoArgs, _pi_info
        )
        groups = {g: g in ("system", "shell") for g in GROUPS}
        manifest = NodeManifest(groups=groups, trust_core_for_high=True)
        self.gate = NodeGate(tools, manifest, None, receipts=self.verifier)

    def offered(self) -> dict[str, str]:
        return {t.name: t.risk for t in self.gate.offered()}

    async def call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        try:
            meta = NoxCallMeta.model_validate(params.get("_nox") or {})
        except ValueError:
            return _refusal("invalid_meta")
        arguments = normalize_arguments(params.get("arguments"))
        decision = self.gate.check(name, arguments, call_id=meta.call_id, meta=meta)
        if not decision.allowed or decision.tool is None or decision.args is None:
            return _refusal(decision.reason)
        if decision.needs_local_confirmation:
            # Headless: only what a person confirmed on the core, never a rule or a grant.
            if meta.confirm_on_device or meta.decided_by != "user":
                return _refusal("high_needs_local_confirmation")
        try:
            result = await decision.tool.handler(decision.args)
        except NodeRefusal as exc:
            return _refusal(exc.reason)
        text = json.dumps(result, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "structuredContent": result}


def _refusal(reason: str) -> dict[str, Any]:
    return {
        "isError": True,
        "_nox": {"refused": reason},
        "content": [{"type": "text", "text": f"refused on the device: {reason}"}],
    }


class WorldNodes(NodeRegistry):
    """The core's node registry with exactly the simulated nodes a task asks for."""

    def __init__(self, folder: Path) -> None:
        signer = ReceiptSigner(folder)
        super().__init__(None, None, signer=signer, core_id=lambda: CORE_ID)  # type: ignore[arg-type]
        verifier = ReceiptVerifier(signer.public_key, device_id=PI_ID, core_id=CORE_ID)
        self.pi = SimulatedNode(verifier)
        self._present: dict[str, NodeEntry] = {}

    def show(self, names: list[str]) -> None:
        """Make the named simulated nodes exist (online) for the next task, and only those."""
        self._present = {}
        if PI_NAME.lower() in (n.lower() for n in names):
            entry = NodeEntry(device_id=PI_ID, name=PI_NAME, online=True, confirmer=False)
            entry.tools = self.pi.offered()
            entry.session = _Session(_Client(self.pi))  # type: ignore[assignment]
            self._present[PI_ID] = entry

    @property
    def devices(self) -> frozenset[str]:
        return frozenset(self._present)

    def entries(self) -> list[NodeEntry]:
        return list(self._present.values())

    def any_online_with(self, tool: str) -> bool:
        return any(e.offers(tool) for e in self._present.values())
