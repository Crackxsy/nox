"""The simulated world: a headless core on free ports, a sandbox folder, a fake web server.

The world also replaces what would reach past the machine or into the room: Nox's browser is wired
to the fake web (every host under `kann-alles.test` is the fake server, everything else does not
resolve - the benchmark has no internet), the screen and the camera return fixed fake images, and
`vision.describe` answers from those images' known descriptions instead of a real model. Only then
does the confirmation policy treat `browser`, `screen` and `camera` as staying in the world.

Only public, long-standing APIs of the core are used (`NoxCore`, `load_config`, the orchestrator's
`handle_text`, the executor's `call`, the permission engine's `reply`), so the same harness measures
an older checkout as a baseline without changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import ipaddress
import os
import shutil
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psutil

from kann_alles.confirm import ConfirmDecision, decide
from kann_alles.skill_builder import WorldSkillBuilder
from kann_alles.tasks import Environment
from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.events import E, Event

START_TIMEOUT_S = 180.0
#: Environment the world replaces while it runs, and restores afterwards.
ISOLATED_ENV = ("NOX_HOME", "NOX_USER_CONFIG", "NOX_COMPUTER_USE_DRY_RUN")
MCP_SERVER = Path(__file__).resolve().parent / "envs" / "mcp_server.py"
STOP_TIMEOUT_S = 30.0
WEB_ROOT = Path(__file__).resolve().parent / "envs" / "web"
SENSES_ROOT = Path(__file__).resolve().parent / "envs" / "senses"
#: The fake web as the browser sees it: a public-looking name, answered by `FakeWeb`.
SITE_DOMAIN = "kann-alles.test"
SITE_URL = f"http://web.{SITE_DOMAIN}"
#: A public documentation-range-like address NetGuard accepts; it is never dialed.
_SITE_IP = "93.184.216.34"
FAKE_VISION_MODEL = "kann-alles-fake-vision"


def free_port_pair(attempts: int = 50) -> int:
    """A `base` such that `base` and `base + 1` were bindable a moment ago (OS-chosen)."""
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            base = int(probe.getsockname()[1])
        if base + 1 > 65535:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second:
                second.bind(("127.0.0.1", base + 1))
        except OSError:
            continue
        return base
    raise RuntimeError("no free port pair")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return


class FakeWeb:
    """Static pages (timetable, shop, weather) on a loopback port, served from `envs/web`."""

    def __init__(self, root: Path = WEB_ROOT) -> None:
        handler = functools.partial(_QuietHandler, directory=str(root))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@dataclass
class ToolCallRecord:
    name: str
    ok: bool
    error: str
    started_s: float
    duration_ms: float


@dataclass
class Probe:
    """What one turn did, observed from outside: tool calls, confirmations, speech."""

    calls: list[ToolCallRecord] = field(default_factory=list)
    confirmations: int = 0
    first_say_s: float | None = None
    said: list[str] = field(default_factory=list)
    decisions: list[ConfirmDecision] = field(default_factory=list)
    #: Tools the model provider ran itself (e.g. web search), reported by the turn.
    provider_tools: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.calls.clear()
        self.provider_tools.clear()
        self.decisions.clear()
        self.confirmations = 0
        self.first_say_s = None
        self.said.clear()


class RecordingSpeaker:
    """Stands in for the voice worker: notes when the first sentence was handed over to speech."""

    def __init__(self, probe: Probe) -> None:
        self._probe = probe

    async def say(self, request: Any) -> None:
        if self._probe.first_say_s is None:
            self._probe.first_say_s = time.perf_counter()
        self._probe.said.append(str(getattr(request, "text", "")))

    async def interrupt(self, *, reason: str) -> None:
        return


class World:
    """One booted core plus its sandbox, reused across tasks and reset between them."""

    def __init__(
        self,
        root: Path,
        *,
        privacy_mode: str = "balanced",
        log_level: str = "WARNING",
        isolated: bool = False,
    ) -> None:
        self.root = root
        self.sandbox = root / "sandbox"
        self.vault = root / "vault"
        self.web = FakeWeb()
        self.probe = Probe()
        self.confirm_tools: list[str] = []
        self.forbidden_tools: list[str] = []
        self.approve_high: list[str] = []
        self._activate_skills = False
        self._privacy_mode = privacy_mode
        self._log_level = log_level
        #: Only true in a declared isolated environment (VM/container). It is the one thing that
        #: lets the harness approve a HIGH-risk dialog (shell, package); off, those stay denied.
        self._isolated = isolated
        self.core: NoxCore | None = None
        #: Tool families whose backends this world replaced by simulations (see the docstring).
        self.simulated: frozenset[str] = frozenset()
        self._games: list[str] = []
        #: The simulated nodes (`kann_alles.nodes`), set up the first time a task asks for one.
        self.nodes: Any = None
        self.simulated_devices: frozenset[str] = frozenset()
        self._saved_env: dict[str, str | None] = {}
        self._unsub: Callable[[], None] | None = None

    def _config(self) -> Any:
        base = free_port_pair()
        for folder in (self.sandbox, self.vault):
            folder.mkdir(parents=True, exist_ok=True)
        paths = {
            name: str(self.root / name)
            for name in ("data", "index", "db", "cache", "backups", "runtime", "logs")
        }
        overrides = {
            "paths": {
                "data_dir": paths["data"],
                "vault_dir": str(self.vault),
                "index_dir": paths["index"],
                "database_dir": paths["db"],
                "cache_dir": paths["cache"],
                "backups_dir": paths["backups"],
                "runtime_dir": paths["runtime"],
                "logs_dir": paths["logs"],
            },
            "ipc": {"port": base, "http_port": base + 1},
            "privacy": {"mode": self._privacy_mode},
            "health": {"check_interval_s": 3600},
            "logging": {"level": self._log_level},
            # Deleting goes through the recycle bin by default; a benchmark must not fill the real
            # one, so there is no delete tool in this world (a task that needs one says so).
            "files": {"roots": [str(self.sandbox)], "delete_to_recycle_bin": False},
            "plugins": {"enabled": []},
            # Never the real keyring: a file store inside this world's own NOX_HOME (`isolate`).
            "security": {"secret_store": "file"},
        }
        return load_config(DEFAULTS_PATH, None, None, overrides)

    def isolate(self) -> None:
        """Point everything that would resolve the user's own Nox folder at this world instead:
        `NOX_HOME` (secrets file, marker, user config) and no user config override."""
        home = self.root / "nox-home"
        home.mkdir(parents=True, exist_ok=True)
        self._saved_env = {key: os.environ.get(key) for key in ISOLATED_ENV}
        os.environ["NOX_HOME"] = str(home)
        os.environ.pop("NOX_USER_CONFIG", None)
        # Computer use runs every guard but never sends input from the benchmark's world.
        os.environ["NOX_COMPUTER_USE_DRY_RUN"] = "1"

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    async def start(self) -> NoxCore:
        self.isolate()
        self.web.start()
        core = NoxCore(self._config(), voice=False, profiles_dir=PROFILES_DIR)
        await asyncio.wait_for(core.start(), timeout=START_TIMEOUT_S)
        self.core = core
        self._games = list(core.config.sensors.game.process_names)
        assert core.orchestrator is not None and core.bus is not None
        core.orchestrator.speaker = RecordingSpeaker(self.probe)
        self._wrap_executor(core)
        await self.simulate(core)
        skills = self._skills()
        if skills is not None:
            # Learning in this world writes a known plugin; see `kann_alles.skill_builder`.
            skills.builder = WorldSkillBuilder()
        self._unsub = core.bus.subscribe(E.SECURITY_PERMISSION_REQUESTED, self._on_confirm)
        return core

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
        if self.core is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.core.stop(), timeout=STOP_TIMEOUT_S)
        self.web.stop()
        self._restore_env()

    def _wrap_executor(self, core: NoxCore) -> None:
        executor = core.tool_executor
        assert executor is not None
        original = executor.call
        probe = self.probe

        async def recording_call(*args: Any, **kwargs: Any) -> Any:
            name = str(kwargs.get("name", args[1] if len(args) > 1 else ""))
            started = time.perf_counter()
            result = await original(*args, **kwargs)
            probe.calls.append(
                ToolCallRecord(
                    name=name,
                    ok=bool(getattr(result, "ok", False)),
                    error=str(getattr(result, "error", "") or ""),
                    started_s=started,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            )
            return result

        # Instance attribute over the method: every holder of this executor records through it.
        executor.call = recording_call  # type: ignore[method-assign]

    def _on_confirm(self, event: Event) -> None:
        """Answer a dialog by the rules in `kann_alles.confirm` (deny unless proven harmless)."""
        assert self.core is not None and self.core.security is not None
        payload = event.payload
        verdict = decide(
            tool=str(payload.get("tool", "")),
            action=str(payload.get("action", "")),
            risk=str(payload.get("risk", "")),
            target=str(payload.get("target", "")),
            confirm_tools=self.confirm_tools,
            forbidden_tools=self.forbidden_tools,
            sandbox=self.sandbox,
            root=self.root,
            isolated=self._isolated,
            simulated=self.simulated,
            approve_high=self.approve_high,
            device=str(payload.get("device") or ""),
            simulated_devices=self.simulated_devices,
        )
        self.probe.confirmations += 1
        self.probe.decisions.append(verdict)
        self.core.security.engine.reply(
            str(payload.get("request_id", "")), verdict.decision, by="eval"
        )

    # -- per task -----------------------------------------------------------------------------

    @staticmethod
    def empty_env() -> Environment:
        return Environment()

    async def prepare(
        self,
        env: Environment,
        *,
        confirm_tools: list[str] | None = None,
        forbidden_tools: list[str] | None = None,
        fresh_session: bool = True,
        approve_high: list[str] | None = None,
    ) -> None:
        """Reset the sandbox, seed files/notes/reminders and (by default) start a new
        conversation, so no task sees what the previous one talked about."""
        assert self.core is not None and self.core.orchestrator is not None
        self.probe.reset()
        self.confirm_tools = list(confirm_tools or [])
        self.forbidden_tools = list(forbidden_tools or [])
        self.approve_high = list(approve_high or [])
        await self._prepare_hands(env)
        self._prepare_nodes(env)
        shutil.rmtree(self.sandbox, ignore_errors=True)
        self.sandbox.mkdir(parents=True)
        for relative, content in env.files.items():
            target = self.sandbox / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for relative, content in env.vault.items():
            target = self.vault / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._reset_reminders(env.reminders)
        if fresh_session:
            session_id = uuid.uuid4().hex
            self.core.orchestrator.session_id = session_id
            assert self.core.bus is not None
            # The turn store records into the session row that this event creates.
            await self.core.bus.publish(
                Event(name=E.SESSION_STARTED, payload={"session_id": session_id})
            )

    def _reset_reminders(self, seeded: list[tuple[str, int]]) -> None:
        assert self.core is not None and self.core.db is not None
        db = self.core.db
        db.execute("DELETE FROM reminders")
        now = datetime.now(UTC)
        for text, minutes in seeded:
            db.execute(
                "INSERT INTO reminders (id, text, due_at, created_at, source) "
                "VALUES (?, ?, ?, ?, 'eval')",
                (
                    uuid.uuid4().hex,
                    text,
                    (now + timedelta(minutes=minutes)).isoformat(),
                    now.isoformat(),
                ),
            )

    # -- MCP servers, skills, watchers, computer use ------------------------------------------

    def _skills(self) -> Any:
        assert self.core is not None
        runtime = (self.core.extensions or {}).get("skills")
        return getattr(runtime, "service", None)

    def _mcp(self) -> Any:
        assert self.core is not None
        runtime = (self.core.extensions or {}).get("mcp")
        client = getattr(runtime, "client", None)
        return getattr(client, "manager", None)

    async def _prepare_hands(self, env: Environment) -> None:
        """Per task: only the fake MCP servers it names, no skills or watchers left from the last
        task, computer use and the fake game exactly as it asks."""
        assert self.core is not None and self.core.db is not None
        from nox.core.config.hands import McpServerConfig  # noqa: PLC0415 - newer checkouts only

        manager = self._mcp()
        if manager is not None:
            for server_id in manager.server_ids():
                await manager.remove(server_id, by="eval")
            for name in env.mcp_servers:
                cfg = McpServerConfig(
                    id=name,
                    name=f"Fake {name}",
                    command=[sys.executable, str(MCP_SERVER), name, self.web.url],
                    network=False,
                    read_only_tools=["fetch"] if name == "fetch" else [],
                )
                await manager.add(cfg, by="eval")
                await manager.approve(name, by="eval")
        skills = self._skills()
        if skills is not None:
            await skills.wait()
            for record in skills.records():
                await skills.forget(record.id, by="eval")
        with contextlib.suppress(Exception):
            self.core.db.execute("DELETE FROM watchers")
        config = self.core.config
        config.desktop.computer_use.enabled = env.computer_use
        config.sensors.game.process_names = (
            [psutil.Process().name()] if env.fake_game else self._games
        )
        self._activate_skills = env.activate_skills

    def _prepare_nodes(self, env: Environment) -> None:
        """Exactly the simulated nodes the task names are online. The core's routes for nodes are
        installed the first time a task needs one (an older checkout without nodes: none)."""
        if env.nodes and self.nodes is None:
            try:
                from kann_alles.nodes import WorldNodes  # noqa: PLC0415 - newer checkouts only
                from nox.node.install import _install_routes  # noqa: PLC0415
            except ImportError:
                return
            self.nodes = WorldNodes(self.root / "node-receipts")
            _install_routes(self.core, self.nodes)
        if self.nodes is not None:
            self.nodes.show(list(env.nodes))
            self.simulated_devices = self.nodes.devices

    async def settle(self) -> None:
        """Before checks: let a skill build finish, then click "Aktivieren" if the task says so."""
        skills = self._skills()
        if skills is None:
            return
        await skills.wait()
        if self._activate_skills:
            for record in skills.records():
                if record.state.value == "ready":
                    await skills.activate(record.id, pin=None, by="eval-user")

    def skill_states(self) -> dict[str, str]:
        skills = self._skills()
        if skills is None:
            return {}
        return {record.id: record.state.value for record in skills.records()}

    def sql_rows(self, query: str) -> list[tuple[Any, ...]]:
        assert self.core is not None and self.core.db is not None
        return [tuple(row) for row in self.core.db.fetch_all(query)]

    def render(self, text: str) -> str:
        """Fill the placeholders a prompt may use: `{web}` (fake web server on loopback, for
        `web.fetch`) and `{site}` (the same pages as Nox's browser reaches them)."""
        return text.replace("{web}", self.web.url).replace("{site}", SITE_URL)

    async def simulate(self, core: Any) -> None:
        """Swap the browser, screen, camera and vision backends for the world's simulations."""
        hands = getattr(core, "extensions", {}).get("hands")
        simulated: set[str] = set()
        browser = getattr(hands, "browser", None)
        if browser is not None:
            port = self.web.port

            def resolver(host: str, _port: int) -> list[Any]:
                name = host.strip("[]").lower().rstrip(".")
                with contextlib.suppress(ValueError):
                    return [ipaddress.ip_address(name)]  # literal: NetGuard judges it as is
                if name == SITE_DOMAIN or name.endswith("." + SITE_DOMAIN):
                    return [ipaddress.ip_address(_SITE_IP)]
                raise OSError(f"{name}: the benchmark world has no internet")

            async def dial(_host: str, _port: int) -> Any:
                return await asyncio.open_connection("127.0.0.1", port)

            await browser.use_network(resolver=resolver, open_connection=dial)
            simulated.add("browser")
        senses = getattr(hands, "senses", None)
        if senses is not None:
            images = {
                kind: (SENSES_ROOT / f"{kind}.png").read_bytes() for kind in ("screen", "camera")
            }
            described = {
                hashlib.sha256(png).hexdigest(): (SENSES_ROOT / f"{kind}.txt").read_text("utf-8")
                for kind, png in images.items()
            }

            def grabber(png: bytes) -> Callable[[], Any]:
                async def grab() -> bytes:
                    return png

                return grab

            async def model() -> str | None:
                return FAKE_VISION_MODEL

            async def describe(png: bytes, _prompt: str) -> str:
                return described.get(hashlib.sha256(png).hexdigest(), "Ein Bild ohne Inhalt.")

            senses.screen = grabber(images["screen"])
            senses.camera = grabber(images["camera"])
            senses.local_model = model
            senses.local_vision = describe
            senses.cloud_vision = None
            # The fake camera may be "on": no device is ever opened in this world.
            await core.security.privacy.set_capture(camera=True, by="eval")
            simulated |= {"screen", "camera"}
        self.simulated = frozenset(simulated)
