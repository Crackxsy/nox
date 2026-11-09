"""Simulated nodes for the benchmark world: "Wohnzimmer-Pi", "Laptop" and "Arbeits-PC".

The core under test routes to them exactly as to real nodes - the router, the device-scoped
permission request, a signed receipt per call - and on each device every call passes the real
`NodeGate` (hard prohibitions, the game lock, stop state, capability groups, risk ceiling,
receipt, budget) and the real node tools. Only what would touch a machine is a fake, so nothing
ever runs on, shows on or sounds from the machine the benchmark runs on:

* the transport: calls are handed to the device in-process instead of over mTLS;
* the sandbox: `FakeSandbox` answers a few well-known commands (`df`, the temperature,
  `systemctl`) with fixed text, lets `sleep N` wait without a process, and refuses everything else;
* the platform layers: system values are fixed per device, a notice is recorded instead of shown,
  music is a number that goes up and down, the screen is a few fixed bytes, the window probe and
  the input sender are fakes (`DryRunSender` sends nothing), and the game lock reads a fixed
  process list. Nox's own headless browser and the process tools (`desktop.processes`,
  `desktop.process_stop`) are not offered on a simulated device at all.

The devices:

* **Wohnzimmer-Pi** - headless Linux; `system` and `shell` enabled. High risk is confirmed on the
  core (one dialog the harness answers) and the Pi accepts that only because its manifest says
  `trust_core_for_high`, as a real headless node would after `nox node allow trust_core_for_high`.
* **Laptop** - a Linux laptop on battery; `system`, `notify`, `media` and `screen` enabled.
* **Arbeits-PC** - a Windows desktop; `system`, `notify`, `media`, `screen`, `desktop` and
  `computer` enabled, `files` and `shell` off. The fictitious game "Starfall Arena" is on its own
  watched-game list and runs in front, so its game lock holds.

Laptop and Arbeits-PC have a screen: high risk there is asked on the device itself (spec C §1.6
step 5), and the person at the device in this world always says no - the same deny-by-default the
harness applies to every dialog on the core.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nox.node.gate import NodeGate
from nox.node.hands import NodeHands, hand_tools
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.protocol import NoxCallMeta
from nox.node.receipts import ReceiptSigner, ReceiptVerifier, normalize_arguments
from nox.node.registry import NodeEntry, NodeRegistry
from nox.node.tools import NoArgs, NodeRefusal, NodeTool, default_tools
from nox.sandbox import Result, SandboxMode
from nox.tools.executor import CALL_DECISION, CallDecision

__all__ = [
    "BUSY_COMMAND",
    "FakeMedia",
    "FakeNotifier",
    "FakePiSandbox",
    "FakeSandbox",
    "GAME_PROCESS",
    "LAPTOP_ID",
    "LAPTOP_NAME",
    "NODE_NAMES",
    "NodeProfile",
    "NodeRun",
    "PI_ID",
    "PI_NAME",
    "PROFILES",
    "SimulatedNode",
    "WORK_PC_ID",
    "WORK_PC_NAME",
    "WorldNodes",
]

PI_NAME = "Wohnzimmer-Pi"
PI_ID = "5e1f" * 8
LAPTOP_NAME = "Laptop"
LAPTOP_ID = "1a7f" * 8
WORK_PC_NAME = "Arbeits-PC"
WORK_PC_ID = "a4b1" * 8
NODE_NAMES = (PI_NAME, LAPTOP_NAME, WORK_PC_NAME)
CORE_ID = "c0de" * 16
#: When an offline device was last seen (shown in the "Geräte" line).
OFFLINE_SINCE = datetime(2026, 10, 20, 14, 2, tzinfo=UTC)
#: The long call a task can have running on a device when it starts (`Environment.node_busy`):
#: the fake sandbox waits on the event loop, no process is started.
BUSY_COMMAND = "sleep 600"
#: How long a busy call may take to reach the device before the task starts anyway.
BUSY_START_S = 1.0
#: A fictitious game: on the Arbeits-PC's own watched list, running and in front.
GAME_PROCESS = "StarfallArena.exe"
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"kann-alles fake screen" * 8
#: Device tools a simulated device does not offer: Nox's own browser (a real Chromium), and the
#: process list and process stop, which would read or end processes of the benchmark machine.
_NOT_SIMULATED = ("browser.", "desktop.processes", "desktop.process_stop")

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
_SLEEP = re.compile(r"(?:\S*[\\/])?sleep\s+(\d{1,4})")


class FakeSandbox:
    """Stands in for a device's sandbox (bwrap / AppContainer): fixed answers, nothing is ever
    executed. `sleep N` waits on the event loop (cancellable), so a stop has something to end."""

    mode = SandboxMode.LINUX_BWRAP

    def __init__(self) -> None:
        self.runs: list[list[str]] = []
        #: Set while a `sleep` waits in here (`WorldNodes.start_busy` waits for it).
        self.sleeping = asyncio.Event()

    def check_usable(self) -> None:
        return None

    async def run(self, argv: list[str], **_kwargs: Any) -> Result:
        self.runs.append(list(argv))
        shell_line = len(argv) > 1 and argv[-2] in ("-c", "-Command")
        line = (argv[-1] if shell_line else " ".join(argv)).strip().lower()
        sleep = _SLEEP.fullmatch(line)
        if sleep is not None:
            self.sleeping.set()
            try:
                await asyncio.sleep(min(int(sleep[1]), 600))
            finally:
                self.sleeping.clear()
            return Result(0, "", "")
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
        return Result(127, "", "not available on the simulated device\n")


#: The name of the first version, which only had the Pi; kept for older imports.
FakePiSandbox = FakeSandbox


class FakeNotifier:
    """`notify.show` without a toast: the notice is only recorded."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    async def show(self, title: str, text: str) -> None:
        self.shown.append((title, text))


class FakeMedia:
    """`media.control` without media keys: a volume number and the actions asked for."""

    def __init__(self, volume: int = 60) -> None:
        self.start = volume
        self.volume = volume
        self.actions: list[str] = []

    def reset(self) -> None:
        self.volume = self.start
        self.actions.clear()

    async def control(self, action: str, level: int | None) -> dict[str, Any]:
        self.actions.append(action)
        if action == "volume" and level is not None:
            self.volume = level
        elif action == "louder":
            self.volume = min(100, self.volume + 10)
        elif action == "quieter":
            self.volume = max(0, self.volume - 10)
        return {"ok": True, "action": action, "volume": self.volume}


class _FakeWindowProbe:
    """The window probe of a simulated PC: one fixed window, every change refused."""

    def windows(self) -> list[Any]:
        from nox.desktop.win32 import WindowInfo  # noqa: PLC0415

        return [
            WindowInfo(
                handle=7,
                title="Starfall Arena",
                process_name=GAME_PROCESS,
                pid=4711,
                minimized=False,
            )
        ]

    def focus(self, _handle: int) -> bool:
        return False

    minimize = restore = close = focus


class _NoBrowser:
    """Nox's own browser is not simulated on a device: its tools are not offered there."""

    async def close(self) -> None:
        return None


@dataclass(frozen=True)
class NodeProfile:
    """One simulated device: what it is, what its manifest enables and what runs on it."""

    name: str
    device_id: str
    groups: frozenset[str]
    #: Has a screen and asks the person there for high risk (`NodeEntry.confirmer`). In this
    #: world that person always says no.
    confirmer: bool
    trust_core_for_high: bool
    windows: bool
    info: Mapping[str, Any]
    #: Whether the C6 tools (screen, desktop, computer use, media) exist on this device.
    world: bool = True
    #: This device's own watched-game list (`node.yaml` `games`).
    games: tuple[str, ...] = ()
    #: Programs running on the device (lower case) and the one in front.
    running: frozenset[str] = frozenset()
    foreground: str = "explorer.exe"


PROFILES: dict[str, NodeProfile] = {
    PI_NAME: NodeProfile(
        name=PI_NAME,
        device_id=PI_ID,
        groups=frozenset({"system", "shell"}),
        confirmer=False,
        trust_core_for_high=True,
        windows=False,
        world=False,
        info={
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
        },
    ),
    LAPTOP_NAME: NodeProfile(
        name=LAPTOP_NAME,
        device_id=LAPTOP_ID,
        groups=frozenset({"system", "notify", "media", "screen"}),
        confirmer=True,
        trust_core_for_high=False,
        windows=False,
        info={
            "os": "Linux",
            "os_version": "6.8.0-45-generic",
            "arch": "x86_64",
            "cpu_count": 8,
            "cpu_percent": 12.0,
            "memory_total_mb": 15_760,
            "memory_used_percent": 44.0,
            "disk_total_gb": 476.9,
            "disk_free_gb": 211.4,
            "uptime_s": 15_300,
            "battery_percent": 64,
            "battery_plugged": False,
            "temperature_c": 47.0,
        },
        running=frozenset({"firefox", "nautilus"}),
        foreground="firefox",
    ),
    WORK_PC_NAME: NodeProfile(
        name=WORK_PC_NAME,
        device_id=WORK_PC_ID,
        groups=frozenset({"system", "notify", "media", "screen", "desktop", "computer"}),
        confirmer=True,
        trust_core_for_high=False,
        windows=True,
        info={
            "os": "Windows",
            "os_version": "11",
            "arch": "AMD64",
            "cpu_count": 16,
            "cpu_percent": 38.0,
            "memory_total_mb": 32_680,
            "memory_used_percent": 57.0,
            "disk_total_gb": 931.5,
            "disk_free_gb": 402.7,
            "uptime_s": 22_800,
        },
        games=(GAME_PROCESS,),
        running=frozenset({GAME_PROCESS.lower(), "explorer.exe", "musicplayer.exe"}),
        foreground=GAME_PROCESS,
    ),
}


@dataclass
class NodeRun:
    """One call whose handler ran on a simulated device (after the device's gate allowed it)."""

    tool: str
    started_s: float
    ended_s: float | None = None
    #: Ended by a stop on the device (`nox/kill`), not by itself.
    stopped: bool = False


def _refusal(reason: str) -> dict[str, Any]:
    return {
        "isError": True,
        "_nox": {"refused": reason},
        "content": [{"type": "text", "text": f"refused on the device: {reason}"}],
    }


def _failed(detail: str) -> dict[str, Any]:
    return {"isError": True, "content": [{"type": "text", "text": detail}]}


class SimulatedNode:
    """The device side of one simulated node: the real gate and tools, fake platform layers."""

    def __init__(self, profile: NodeProfile, verifier: ReceiptVerifier) -> None:
        self.profile = profile
        self.sandbox = FakeSandbox()
        self.notifier = FakeNotifier()
        self.media = FakeMedia()
        #: Every handler that ran here in this task, in order.
        self.runs: list[NodeRun] = []
        #: High-risk calls the person at this device was asked about (and said no to).
        self.dialogs: list[str] = []
        #: Why this device refuses everything right now (the core's stop), or None.
        self.stopped: str | None = None
        self._calls: set[asyncio.Future[dict[str, Any]]] = set()
        self._lock: Any = None
        sandbox = self.sandbox
        hands = NodeHands(folders=lambda: [], protected=(), sandbox=lambda: sandbox)  # type: ignore[arg-type, return-value]
        tools: dict[str, NodeTool] = hand_tools(hands)
        tools["notify.show"] = default_tools(self.notifier)["notify.show"]
        info = dict(profile.info)

        async def system_info(_args: BaseModel) -> dict[str, Any]:
            return dict(info)

        tools["system.info"] = NodeTool(
            "system.info", "system", "read", "System values of this device.", NoArgs, system_info
        )
        manifest = NodeManifest(
            groups={g: g in profile.groups for g in GROUPS},
            trust_core_for_high=profile.trust_core_for_high,
            games=list(profile.games),
        )
        if profile.world:
            tools.update(self._world_tools(manifest))
        self.gate = NodeGate(tools, manifest, None, stopped=self._stop_state, receipts=verifier)
        # Without the C6 tools there is no lock to read: input stays refused (fail closed).
        if self._lock is not None:
            self.gate.input_lock = self._lock.reason

    def _stop_state(self) -> str | None:
        return self.stopped

    def _world_tools(self, manifest: NodeManifest) -> dict[str, NodeTool]:
        """The C6 tools with fakes behind them; on an older checkout without them: none."""
        try:
            from nox.desktop.computer_use import DryRunSender  # noqa: PLC0415
            from nox.node.world import GameLock, World, default_games, world_tools  # noqa: PLC0415
        except ImportError:
            return {}
        profile = self.profile

        async def notice(_text: str) -> None:
            return None  # shown nowhere: a fake device has no screen to show it on

        async def grab() -> bytes:
            return FAKE_PNG

        self._lock = GameLock(
            watched=lambda: (*default_games(), *profile.games),
            running_names=lambda: set(profile.running),
            foreground_name=lambda: profile.foreground,
            process_pids=lambda names: {4711} if names & profile.running else set(),
            services=set,
            screen_state=lambda: 5,  # QUNS_ACCEPTS_NOTIFICATIONS: nothing else owns the screen
            windows=profile.windows,
        )
        world = World(
            notice=notice,
            computer_enabled=lambda: "computer" in manifest.enabled() and self.stopped is None,
            dialog_open=lambda: False,
            browser_dir=Path("unused-simulated-browser"),
            lock=self._lock,
            screen=grab,
            camera=grab,
            media=self.media,
            windows=profile.windows,
            probe=_FakeWindowProbe,
            sender=DryRunSender,
            browser=_NoBrowser(),
        )
        return {
            name: tool
            for name, tool in world_tools(world).items()
            if not name.startswith(_NOT_SIMULATED)
        }

    def offered(self) -> dict[str, str]:
        return {t.name: t.risk for t in self.gate.offered()}

    def busy(self) -> bool:
        return any(not task.done() for task in self._calls)

    def reset(self) -> None:
        """A fresh device for the next task (`WorldNodes.clear` ends running calls first)."""
        self.stopped = None
        self.runs.clear()
        self.dialogs.clear()
        self.sandbox.runs.clear()
        self.notifier.shown.clear()
        self.media.reset()

    def cancel_all(self) -> list[asyncio.Future[dict[str, Any]]]:
        running = list(self._calls)
        for task in running:
            task.cancel()
        return running

    async def stop(self, reason: str = "stopped_by_core") -> None:
        """`nox/kill` on the device: refuse everything from now on, end every running call."""
        self.stopped = reason
        await asyncio.gather(*self.cancel_all(), return_exceptions=True)

    def resume(self) -> None:
        """`nox/resume`: the core's stop ends."""
        self.stopped = None

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
            refusal = self._confirm_here(name, meta)
            if refusal is not None:
                return _refusal(refusal)
        run = NodeRun(name, time.perf_counter())
        self.runs.append(run)
        work = asyncio.ensure_future(decision.tool.handler(decision.args))
        self._calls.add(work)
        try:
            result = await work
        except asyncio.CancelledError:
            run.stopped = True
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the caller went away; not a stop on this device
            return _refusal(self.stopped or "stopped")
        except NodeRefusal as exc:
            return _refusal(exc.reason)
        except Exception as exc:  # noqa: BLE001 - a failing tool is an answer, as on a real node
            return _failed(type(exc).__name__)
        finally:
            run.ended_s = time.perf_counter()
            self._calls.discard(work)
        text = json.dumps(result, ensure_ascii=False, default=str)
        return {"content": [{"type": "text", "text": text}], "structuredContent": result}

    def _confirm_here(self, name: str, meta: NoxCallMeta) -> str | None:
        """High risk: the person at the device decides; a headless device trusts only a person's
        yes on the core, and only when its manifest says so (as `nox.node.agent` does)."""
        if self.profile.confirmer:
            self.dialogs.append(name)
            return "denied_on_device"  # the person at the device in this world always says no
        trusted = self.gate.manifest.trust_core_for_high
        if trusted and not meta.confirm_on_device and meta.decided_by == "user":
            return None
        return "high_needs_local_confirmation"


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
    blobs: dict[str, bytes] = field(default_factory=dict)

    def take_blob(self, call_id: str) -> bytes | None:
        return self.blobs.pop(call_id, None)


class WorldNodes(NodeRegistry):
    """The core's node registry with exactly the simulated devices a task asks for: online, or
    paired but offline (a name the user may say; every call to it fails fast as offline)."""

    def __init__(self, folder: Path) -> None:
        signer = ReceiptSigner(folder)
        super().__init__(None, None, signer=signer, core_id=lambda: CORE_ID)  # type: ignore[arg-type]
        self.nodes: dict[str, SimulatedNode] = {
            name: SimulatedNode(
                profile,
                ReceiptVerifier(signer.public_key, device_id=profile.device_id, core_id=CORE_ID),
            )
            for name, profile in PROFILES.items()
        }
        self._present: dict[str, NodeEntry] = {}
        self._busy: set[asyncio.Future[dict[str, Any]]] = set()

    @property
    def pi(self) -> SimulatedNode:
        return self.nodes[PI_NAME]

    @property
    def laptop(self) -> SimulatedNode:
        return self.nodes[LAPTOP_NAME]

    @property
    def work_pc(self) -> SimulatedNode:
        return self.nodes[WORK_PC_NAME]

    def _node(self, name: str) -> SimulatedNode:
        for known, node in self.nodes.items():
            if known.casefold() == name.casefold():
                return node
        raise ValueError(f"no simulated device {name!r} (known: {', '.join(NODE_NAMES)})")

    def show(self, names: Iterable[str], offline: Iterable[str] = ()) -> None:
        """Make the named simulated devices exist for the next task - online, or paired but
        offline - and only those. Every device starts fresh."""
        self._present = {}
        for node in self.nodes.values():
            node.reset()
        for name in names:
            node = self._node(name)
            profile = node.profile
            entry = NodeEntry(
                device_id=profile.device_id,
                name=profile.name,
                online=True,
                confirmer=profile.confirmer,
            )
            entry.tools = node.offered()
            entry.session = _Session(_Client(node), device_id=profile.device_id)  # type: ignore[assignment]
            self._present[profile.device_id] = entry
        for name in offline:
            profile = self._node(name).profile
            if profile.device_id in self._present:
                raise ValueError(f"{profile.name} cannot be online and offline at once")
            entry = NodeEntry(
                device_id=profile.device_id, name=profile.name, confirmer=profile.confirmer
            )
            entry.last_seen = OFFLINE_SINCE
            self._present[profile.device_id] = entry

    def _online(self) -> list[tuple[NodeEntry, SimulatedNode]]:
        by_id = {node.profile.device_id: node for node in self.nodes.values()}
        return [(e, by_id[e.device_id]) for e in self._present.values() if e.online]

    @property
    def devices(self) -> frozenset[str]:
        """The simulated devices a call can reach right now (online ones)."""
        return frozenset(entry.device_id for entry, _node in self._online())

    def entries(self) -> list[NodeEntry]:
        return sorted(self._present.values(), key=lambda e: e.name.lower())

    def any_online_with(self, tool: str) -> bool:
        return any(e.offers(tool) for e in self._present.values())

    # -- what the world reads and does -------------------------------------------------------

    def runs(self) -> tuple[tuple[str, str], ...]:
        """(device name, tool) for every handler that ran on a device in this task."""
        return tuple(
            (node.profile.name, run.tool) for _entry, node in self._online() for run in node.runs
        )

    async def start_busy(self, names: Iterable[str]) -> None:
        """Start a long `shell.run` on each named online device through the real call path (the
        core's receipt, the device's gate), as if a person had confirmed it on the core a moment
        ago, and wait until it runs there."""
        for name in names:
            node = self._node(name)
            entry = self._present.get(node.profile.device_id)
            if entry is None or not entry.online:
                raise ValueError(f"{node.profile.name} must be online to be busy")
            node.sandbox.sleeping.clear()
            task = asyncio.ensure_future(self._busy_call(entry))
            self._busy.add(task)
            task.add_done_callback(self._busy.discard)
            started = asyncio.ensure_future(node.sandbox.sleeping.wait())
            # Waiting in the device's sandbox, or refused there (the task ends), or too slow.
            await asyncio.wait(
                {task, started}, timeout=BUSY_START_S, return_when=asyncio.FIRST_COMPLETED
            )
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)

    async def _busy_call(self, entry: NodeEntry) -> dict[str, Any]:
        token = CALL_DECISION.set(CallDecision(decided_by="user"))
        try:
            return await self.call(
                entry, "shell.run", {"command": BUSY_COMMAND}, risk="high", timeout_s=900
            )
        finally:
            CALL_DECISION.reset(token)

    async def clear(self) -> None:
        """End whatever the last task left running (a busy call) before the next one starts."""
        leftovers = list(self._busy)
        for task in leftovers:
            task.cancel()
        for node in self.nodes.values():
            leftovers += node.cancel_all()
        await asyncio.gather(*leftovers, return_exceptions=True)
        for node in self.nodes.values():
            node.resume()

    async def stop_all(self) -> None:
        """The kill switch's stop hook in this world: `nox/kill` to every simulated device."""
        await asyncio.gather(*(node.stop() for node in self.nodes.values()))

    async def resume_all(self) -> None:
        """The kill switch's resume hook: `nox/resume` to every simulated device."""
        for node in self.nodes.values():
            node.resume()

    def idle(self) -> bool:
        return not any(node.busy() for node in self.nodes.values())

    def stop_seconds(self, since: float) -> float | None:
        """How long after `since` every device call that was running then had been stopped.
        None when none was running, one still runs, or one ended by itself instead."""
        running = [
            run
            for node in self.nodes.values()
            for run in node.runs
            if run.started_s <= since and (run.ended_s is None or run.ended_s >= since)
        ]
        ended = [run.ended_s for run in running if run.ended_s is not None and run.stopped]
        if not running or len(ended) != len(running):
            return None
        return max(ended) - since
