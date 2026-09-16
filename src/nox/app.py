"""Composition root of the core process (`python -m nox.app`, `nox core`, `nox dev`).

Wires config → logging → database → security → bus/state → IPC hub + HTTP → AI router → health →
pet service → orchestrator → voice worker, in the order of the Runtime Lifecycle note. Nothing in
here decides anything on its own: it only constructs and connects the components.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

import nox
from nox.ai.base import AiProvider, ProviderInfo
from nox.ai.config import AiConfig
from nox.ai.prompting import DECIDED_PERSONALITY_BLOCK, build_system_prompt
from nox.ai.providers.claude_code import ClaudeCodeProvider
from nox.ai.providers.ollama import OllamaProvider
from nox.ai.providers.rules import RulesProvider
from nox.ai.router import DefaultRouter
from nox.core.bus import AsyncEventBus
from nox.core.config import ConfigError, NoxConfig, load_config
from nox.core.events import E, Event, HealthStatus
from nox.core.health import Check, HealthService
from nox.core.jobobject import JobObject
from nox.core.logging import configure_logging, get_logger, shutdown_logging
from nox.core.orchestrator import Orchestrator, OrchestratorConfig
from nox.core.speech_policy import SpeechPolicy
from nox.core.state import Mode, PetFunctional, PrivacyMode, SystemLevel
from nox.core.statemgr import NoxStateManager
from nox.data.db import Database
from nox.data.repos import (
    HealthHistoryRepository,
    SessionRepository,
    StateCheckpointRepository,
    TurnRepository,
)
from nox.data.stream_repos import (
    ChatEventRepository,
    FunkenLedgerRepository,
    StreamSessionRepository,
    ViewerRepository,
)
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.http import HttpServer, HttpSettings, create_app
from nox.ipc.server import HubSettings, IpcHub
from nox.ipc.tokens import TokenStore
from nox.pet.service import PetService
from nox.plugins.manager import PluginManager, PluginManagerSettings
from nox.security.model import Decision
from nox.security.service import SecurityContext
from nox.stream.booking import FunkenBooking
from nox.stream.funken import FunkenService
from nox.stream.responder import StreamResponder
from nox.stream.sessions import StreamSessionService
from nox.supervisor.client import SupervisorClient
from nox.tools.builtin import register_v01_tools
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry
from nox.voice.base import Channel, TtsRequest

log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_PATH = REPO_ROOT / "config" / "defaults.yaml"
PROFILES_DIR = REPO_ROOT / "config" / "profiles"
PLUGINS_DIR = REPO_ROOT / "plugins"
PET_DIST = REPO_ROOT / "ui" / "pet" / "dist"
DASHBOARD_DIST = REPO_ROOT / "ui" / "dashboard" / "dist"
GREETING = {"de": "Hallo, ich bin Nox. Ich bin bereit.", "en": "Hi, I am Nox. I am ready."}
#: Budget for the live provider probe behind `ai.providers`, chosen well below the UI clients'
#: 10 s request timeout (`ui/shared/ipc.ts`); a slower probe is answered from health instead.
PROVIDERS_PROBE_BUDGET_S = 3.0


def _retrieve_exception(task: asyncio.Task[Any]) -> None:
    """Consume a background task's exception so asyncio does not report it as never retrieved."""
    if not task.cancelled():
        task.exception()


def decide_greeting(policy: SpeechPolicy) -> bool:
    """OP-10: may the startup greeting be spoken right now?

    A standalone wrapper around `SpeechPolicy.may_speak("greeting")` so the greeting decision is
    testable without booting a `NoxCore` (`_greet_when_voice_ready` calls `may_speak` directly
    instead, since it also needs the denial reason for its log line).
    """
    allowed, _reason = policy.may_speak("greeting")
    return allowed


# ---- IPC payload models ------------------------------------------------------------------------


class StateGet(BaseModel):
    path: str | None = None


class ModeSet(BaseModel):
    mode: Mode


class PrivacySet(BaseModel):
    mode: PrivacyMode | None = None
    microphone: bool | None = None
    screen: bool | None = None
    camera: bool | None = None
    confirmed: bool = False


class SecurityKill(BaseModel):
    reason: str = ""
    origin: str = "ui"


class SecurityPanic(BaseModel):
    origin: str = "ui"


#: Roles that may resume from safe mode (OP-6 D); `supervisor` is the tray/hotkey path.
RESUME_ROLES: frozenset[str] = frozenset({"shell", "dashboard", "supervisor"})
USER_KILL_ORIGINS: frozenset[str] = frozenset(
    {"ui", "shell", "dashboard", "hotkey", "tray", "voice", "supervisor", "pet"}
)


class SecurityResume(BaseModel):
    pin: str | None = None


class PermissionReply(BaseModel):
    grant_id: str
    decision: Decision
    remember: bool = False


class VoicePtt(BaseModel):
    pressed: bool


class VoiceMute(BaseModel):
    muted: bool


class ChatSend(BaseModel):
    text: str
    session_id: str | None = None
    speak: bool = True
    language: str | None = None


class PetInteract(BaseModel):
    type: str = "click"
    x: float | None = None
    y: float | None = None


class WorkerRegister(BaseModel):
    service: str
    capabilities: list[str] = []
    pid: int


class WorkerReady(BaseModel):
    service: str


class WorkerHeartbeat(BaseModel):
    load: float = 0.0
    status: str = "running"


class FunkenTopRequest(BaseModel):
    limit: int = 10


# ---- adapters ----------------------------------------------------------------------------------


class WorkerSpeaker:
    """Orchestrator `Speaker` that forwards TTS to the connected voice worker over the hub."""

    def __init__(self, hub: IpcHub, client_id: Callable[[], str | None]) -> None:
        self._hub = hub
        self._client_id = client_id

    async def say(self, request: TtsRequest) -> None:
        cid = self._client_id()
        if cid is None:
            log.info("speaker.no_voice_worker", utterance_id=request.utterance_id)
            return
        await self._hub.request(cid, "tts.speak", request.model_dump(mode="json"), timeout=120.0)

    async def interrupt(self, *, reason: str) -> None:
        cid = self._client_id()
        if cid is None:
            return
        with contextlib.suppress(IpcError):
            await self._hub.request(cid, "tts.stop", {"reason": reason}, timeout=2.0)


class DbTurnStore:
    """Orchestrator `TurnStore` on the SQLite repositories (text only, retention via privacy)."""

    def __init__(self, turns: TurnRepository, retention_days: int | None) -> None:
        self._turns = turns
        self._retention_days = retention_days

    async def record(
        self, session_id: str, role: str, text: str, *, provider: str = "", latency_ms: int = 0
    ) -> None:
        retain_until = None
        if self._retention_days:
            from datetime import timedelta

            retain_until = datetime.now(UTC) + timedelta(days=self._retention_days)
        await asyncio.to_thread(
            self._turns.add,
            session_id,
            role,
            text,
            provider=provider,
            latency_ms=latency_ms or None,
            retain_until=retain_until,
        )

    async def recent(self, session_id: str, limit: int) -> list[tuple[str, str]]:
        rows = await asyncio.to_thread(self._turns.list_for_session, session_id, 1000)
        return [(r.role, r.text) for r in rows[-limit:]]


@dataclass
class WorkerProcess:
    service: str
    process: subprocess.Popen[bytes] | None
    client_id: str | None = None
    registered: asyncio.Event = field(default_factory=asyncio.Event)


# ---- the core ----------------------------------------------------------------------------------


class NoxCore:
    def __init__(
        self,
        config: NoxConfig,
        *,
        voice: bool = True,
        profiles_dir: Path = PROFILES_DIR,
        pet_dist: Path = PET_DIST,
        dashboard_dist: Path = DASHBOARD_DIST,
        worker_command: list[str] | None = None,
        extensions: bool = True,
    ) -> None:
        self.config = config
        self.voice_enabled = voice
        self.profiles_dir = profiles_dir
        self.pet_dist = pet_dist
        self.dashboard_dist = dashboard_dist
        self.worker_command = worker_command or [sys.executable, "-m", "nox.worker"]
        self.auto_extensions = extensions  # tests that call install() themselves pass False
        self.session_id = uuid.uuid4().hex
        self.stopped = asyncio.Event()
        self.shutdown_requested = asyncio.Event()  # set by sup.stop (B-6); _run() waits on it too
        self._started = False
        self._workers: dict[str, WorkerProcess] = {}
        self._job = JobObject("nox-core-workers")
        self._tasks: set[asyncio.Task[Any]] = set()
        self._providers_probe: asyncio.Task[list[ProviderInfo]] | None = None

    # -- boot -------------------------------------------------------------------------------------
    async def start(self) -> None:
        cfg = self.config
        paths = cfg.paths
        for d in (paths.runtime_dir, paths.logs_dir, paths.database_dir, paths.data_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        configure_logging(
            Path(paths.logs_dir),
            level=cfg.logging.level,
            json_file=cfg.logging.json_output,
            pii=cfg.logging.pii_filter,
            retention_days=cfg.log_retention_days,
        )
        for w in cfg.warnings:
            log.warning("config.layer_rejected", **w.model_dump())
        log.info(
            "core.boot", version=nox.__version__, profile=cfg.profile_id or cfg.security.profile
        )

        # 2. supervisor client - deliberately the first thing after logging: every step below can
        # take seconds on a cold start, and the heartbeat task has to exist before them or the
        # watchdog counts the boot itself as a hang (Runtime Lifecycle step 8, moved up 2026-09-16).
        self.supervisor = SupervisorClient.from_env(
            on_kill=self._on_supervisor_kill,
            on_stop=self._request_shutdown,
            on_restart=self._on_supervisor_restart,
            status_provider=self._supervisor_status,
            interval_s=cfg.supervisor.heartbeat_interval_s,
        )
        if self.supervisor is not None:
            self.supervisor.start()
        else:
            log.warning("supervisor.absent", note="running standalone (dev mode)")
        await asyncio.sleep(0)  # let the heartbeat go out before the heavy steps

        # 3. database (migrations + the sqlite-vec extension: seconds on a cold disk, and
        # synchronous by design - see `nox.data.db`), so it runs in a worker thread.
        self.db = await asyncio.to_thread(self._open_database, Path(paths.database_dir) / "nox.db")
        await asyncio.to_thread(self.db.load_sqlite_vec)

        # 6. bus + state
        self.bus = AsyncEventBus()
        self.state = NoxStateManager(
            self.bus,
            StateCheckpointRepository(self.db),
            checkpoint_interval_s=cfg.health.checkpoint_interval_s,
        )
        restored = await self.state.restore_latest()
        log.info("state.restored" if restored else "state.fresh")

        # 5. security (mandatory)
        await asyncio.sleep(0)
        self.security = SecurityContext.build(
            cfg.model_dump(mode="json", by_alias=True),
            conn=self.db.connection,
            profiles_dir=self.profiles_dir,
            bus=self.bus,
            session_id=self.session_id,
        )
        verification = await asyncio.to_thread(self.security.verify_boot)
        if not getattr(verification, "ok", True):
            log.error("audit.chain_broken", detail=str(verification))
        await self.state.update("privacy.mode", self.security.privacy.mode.value, reason="boot")
        await self.state.update("system.level", SystemLevel.RUNNING.value, reason="boot")

        # 7. IPC hub + HTTP
        self.tokens = TokenStore()
        self.tokens.write_session_token(Path(paths.runtime_dir))
        self.registry = RequestRegistry()
        self._register_handlers()
        ipc_cfg = cfg.ipc.model_dump()
        self.hub = IpcHub(
            HubSettings.from_config(ipc_cfg),
            self.tokens,
            self.registry,
            self.bus,
            Path(paths.runtime_dir),
        )
        await self.hub.start()
        self.http = HttpServer(
            create_app(
                health=self._health_json,
                state=self._state_json_for,
                providers=self._providers_json,
                session_token=lambda: self.tokens.session_token,
                pet_dist=self.pet_dist if self.pet_dist.exists() else None,
                dashboard_dist=self.dashboard_dist if self.dashboard_dist.exists() else None,
            ),
            HttpSettings.from_config(
                ipc_cfg, pet_dist=self.pet_dist, dashboard_dist=self.dashboard_dist
            ),
            runtime_dir=Path(paths.runtime_dir),
        )
        await self.http.start()
        log.info("ipc.ready", ws=self.hub.url, http=self.http.url)

        # 7a. tool registry + executor (Tool Model): one catalogue shared by core and plugins
        self.tool_registry = ToolRegistry()
        self.tool_executor = ToolExecutor(
            self.tool_registry,
            self.security.engine,
            self.security.audit,
            self.bus,
            self.security.killswitch,
        )

        # 7b. plugin manager (built after security + IPC, started after `system.started`)
        self.plugins = PluginManager(
            tool_registry=self.tool_registry,
            plugins_dir=Path(cfg.plugins.dir) if cfg.plugins.dir else PLUGINS_DIR,
            bus=self.bus,
            hub=self.hub,
            tokens=self.tokens,
            registry=self.registry,
            engine=self.security.engine,
            secrets=self.security.secrets,
            audit=self.security.audit,
            job=self._job,
            settings=PluginManagerSettings(enabled=list(cfg.plugins.enabled)),
            worker_command=self.worker_command,
            cwd=REPO_ROOT,
            mode=lambda: str(self.state.get("assistant.mode")),
            safe_mode=self.security.killswitch.is_engaged,
            global_egress_allowlist=tuple(cfg.security.egress_allowlist),
            loopback_allowlist=tuple(cfg.security.loopback_allowlist),
        )
        self.plugins.register_handlers()

        # 10. AI router
        ai_cfg = AiConfig.from_mapping(cfg.ai.model_dump())
        # Every HTTP client in the core goes through the egress guard (Security Model §5): the
        # loopback allow-list decides whether Ollama is reachable in PRIVATE/OFFLINE.
        egress = self.security.egress
        providers: list[AiProvider] = [
            RulesProvider(status_source=lambda: dict(self.state.get("system.health"))),
            OllamaProvider(
                ai_cfg.providers.ollama,
                client_factory=lambda: egress.client(timeout=httpx.Timeout(10.0, connect=5.0)),
            ),
            ClaudeCodeProvider(ai_cfg.providers.claude_code),
        ]
        self.router = DefaultRouter(
            providers,
            self.bus,
            ai_cfg.router,
            cloud_allowed=self.security.privacy.allows_cloud,
        )
        self.ai_providers = providers  # for `ai.providers` when a live probe runs long

        # 9. health
        self.health = HealthService(
            self.bus,
            HealthHistoryRepository(self.db),
            self._health_checks(providers),
            interval_s=cfg.health.check_interval_s,
            state_manager=self.state,
        )
        await self.health.run_once()
        self.health.start()
        register_v01_tools(self.tool_registry, state=self.state, health=self.health)

        # 10. services
        self.pet = PetService(self.bus, self.state)
        await self.pet.start()
        self.speech_policy = SpeechPolicy(
            state=self.state,
            config=cfg,
            active_zone=lambda: self.security.privacy.active_zone,
            privacy_mode=lambda: self.security.privacy.mode.value,
        )
        self.sessions = SessionRepository(self.db)
        await asyncio.to_thread(
            self.sessions.create,
            str(self.state.get("assistant.mode")),
            self.security.privacy.mode.value,
            session_id=self.session_id,
        )
        self.speaker = WorkerSpeaker(self.hub, lambda: self._worker_client("voice"))
        retention = cfg.privacy.retention.raw_transcripts_days or None
        self.orchestrator = Orchestrator(
            bus=self.bus,
            state=self.state,
            router=self.router,
            speaker=self.speaker,
            turns=DbTurnStore(TurnRepository(self.db), retention),
            memory_policy=self.security.privacy,
            system_prompt=self._system_prompt,
            config=OrchestratorConfig(
                default_language=cfg.identity.ui_language,
                channel=Channel(cfg.voice.channels.routing),
            ),
            session_id=self.session_id,
        )
        await self.orchestrator.start()

        # kill switch hooks (below the AI layer)
        ks = self.security.killswitch
        ks.register_stop_hook("orchestrator", lambda: self.orchestrator.cancel(reason="kill"))
        ks.register_stop_hook("voice.stop", self._stop_voice_output)
        ks.register_stop_hook("workers.terminate", self._terminate_workers)
        # Plugin Architecture: every plugin gets `plugin.stop`, then it is terminated. The ack
        # window is kept below the kill switch's own 2 s hook budget so the hook always completes.
        ks.register_stop_hook(
            "plugins.stop", lambda: self.plugins.stop_all("kill_switch", ack_timeout_s=1.5)
        )
        self.bus.subscribe(E.VOICE_KILL_PHRASE, self._on_kill_phrase)
        self.bus.subscribe(E.SECURITY_KILL_SWITCH, self._on_kill_event)
        self.bus.subscribe(E.IPC_CLIENT_DISCONNECTED, self._on_client_disconnected)

        # 10b. Stream Bot core services (Spec v0.2, EPIC-11): session lifecycle, Funken booking,
        # chat responder. Subscribed before plugins start (step 13) so they see every obs.*/
        # twitch.*/stream.* event from the plugins' very first one.
        stream_viewers = ViewerRepository(self.db)
        self.stream_sessions = StreamSessionService(
            self.bus,
            StreamSessionRepository(self.db),
            ChatEventRepository(self.db),
            cfg.stream.chat,
            stream_viewers,
        )
        self.stream_sessions.start()
        self.funken = FunkenService(
            stream_viewers,
            FunkenLedgerRepository(self.db),
            cfg.stream.funken,
            bus=self.bus,
            audit=self.security.audit,
        )
        self.funken_booking = FunkenBooking(
            self.bus,
            self.funken,
            stream_viewers,
            self.tool_executor,
            cfg.stream.funken,
            is_session_active=self.stream_sessions.is_active,
        )
        self.funken_booking.start()
        self.stream_responder = StreamResponder(
            self.bus,
            self.router,
            self.tool_executor,
            cfg.stream.relevance,
            self.security.killswitch,
            facts=self._stream_facts,
        )
        self.stream_responder.start()

        # 11. voice worker
        if self.voice_enabled:
            self._spawn_worker("voice")

        # 11b. release extensions (v0.3–v0.9): each module exposes `install(core)`; a failing
        # extension is reported as unavailable and never aborts the boot (P10: no fake capability).
        # The *import* is the expensive half (`rl` pulls in OpenCV: ~6 s cold) and it is plain
        # blocking CPU/IO, so it runs in a worker thread; `install(core)` itself stays on the loop
        # because it wires bus subscriptions and tasks. Without this the whole loop froze for
        # ~10 s here and the supervisor counted the boot as missed heartbeats (2026-09-15).
        self.extensions: dict[str, Any] = {}
        for name, enabled in (
            ()
            if not self.auto_extensions
            else (
                ("sensors", True),
                ("memory", True),
                ("health", True),
                ("proactive", True),
                ("pm", True),
                ("rl", True),
                ("clips", True),
                ("creative", True),
                ("settings", True),
                ("remote", bool(getattr(getattr(cfg, "remote", None), "enabled", False))),
            )
        ):
            if not enabled:
                continue
            try:
                module = await asyncio.to_thread(importlib.import_module, f"nox.{name}.install")
                self.extensions[name] = module.install(self)
                log.info("extension.installed", extension=name)
            except Exception as exc:  # noqa: BLE001 - degraded, not fatal
                log.error("extension.failed", extension=name, error=str(exc))
                self.extensions[name] = None
            await asyncio.sleep(0)  # one extension per loop iteration

        # 12. started
        self._started = True
        await self.bus.publish(
            Event(name=E.SYSTEM_STARTED, payload={"session_id": self.session_id})
        )
        self._spawn_task(self._greet_when_voice_ready())

        # 13. plugins (after system.started, so a plugin sees a fully booted core)
        await self.plugins.start()
        for check in self.plugins.health_checks():
            self.health.add_check(check)
        log.info("core.started", session_id=self.session_id)

    # -- shutdown ----------------------------------------------------------------------------------
    async def stop(self, reason: str = "shutdown") -> None:
        """Graceful shutdown (B-6): orchestrator cancel, workers stop 2 s then kill, final
        checkpoint, audit `system.stopped`. Budgeted at 6 s by the supervisor's `sup.stop`
        timeout; called directly here regardless of who asked (signal, sup.stop, or a test)."""
        if not self._started:
            return
        self._started = False
        log.info("core.stopping", reason=reason)
        await self.state.update("system.level", SystemLevel.STOPPING.value, reason="stop")
        await self.bus.publish(Event(name=E.SYSTEM_STOPPING))
        for t in list(self._tasks):
            t.cancel()
        for name, runtime in reversed(list(getattr(self, "extensions", {}).items())):
            stop = getattr(runtime, "stop", None)
            if stop is None:
                continue
            try:
                result = stop()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("extension.stop_failed", extension=name, error=str(exc))
        with contextlib.suppress(Exception):
            await self.orchestrator.stop()
        with contextlib.suppress(Exception):
            await self.stream_responder.stop()
        with contextlib.suppress(Exception):
            await self.funken_booking.stop()
        with contextlib.suppress(Exception):
            await self.stream_sessions.stop()
        with contextlib.suppress(Exception):
            await self.pet.stop()
        with contextlib.suppress(Exception):
            await self.health.stop()
        with contextlib.suppress(Exception):
            await self.plugins.stop(reason)
        await self._terminate_workers()
        if self.supervisor is not None:
            with contextlib.suppress(Exception):
                await self.supervisor.stop()
        with contextlib.suppress(Exception):
            await self.http.stop()
        with contextlib.suppress(Exception):
            await self.hub.stop()
        with contextlib.suppress(Exception):
            await self.state.checkpoint(immediate=True, reason="shutdown")
            await self.state.close()
        with contextlib.suppress(Exception):
            self.security.audit.append(
                actor="system",
                tool="core",
                action="system.stopped",
                target="",
                decision="allow",
                result="ok",
                details={"reason": reason},
            )
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.sessions.end, self.session_id)
        self.tokens.remove_session_token()
        self.db.close()
        self._job.close()
        shutdown_logging()
        self.stopped.set()

    # -- helpers -----------------------------------------------------------------------------------
    def _open_database(self, path: Path) -> Database:
        db = Database(path)
        if not db.integrity_check():
            db.close()
            corrupt = path.with_name(f"{path.name}.corrupt-{datetime.now(UTC):%Y%m%d%H%M%S}")
            path.rename(corrupt)
            log.error("db.corrupt_renamed", path=str(corrupt))
            db = Database(path)
        applied = db.migrate()
        if applied:
            log.info("db.migrated", migrations=applied)
        return db

    def _spawn_task(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _system_prompt(self) -> str:
        facts = {
            "mode": str(self.state.get("assistant.mode")),
            "privacy_mode": self.security.privacy.mode.value,
            "user": self.config.identity.user_display_name or "the user",
            "time": datetime.now().astimezone().isoformat(timespec="minutes"),
        }
        return build_system_prompt(DECIDED_PERSONALITY_BLOCK, facts)

    def _stream_facts(self) -> dict[str, object]:
        """Facts for `StreamResponder`'s system prompt (`nox.stream.responder`); mirrors
        `_system_prompt`'s but fixes `mode` to "stream" regardless of `assistant.mode` - a chat
        answer is always in the stream persona even if the assistant itself is in another mode."""
        return {
            "mode": "stream",
            "privacy_mode": self.security.privacy.mode.value,
            "user": self.config.identity.user_display_name or "the user",
            "time": datetime.now().astimezone().isoformat(timespec="minutes"),
        }

    def _health_checks(self, providers: list[AiProvider]) -> list[Check]:
        async def db_check() -> tuple[HealthStatus, str]:
            ok = await asyncio.to_thread(self.db.integrity_check)
            return (HealthStatus.AVAILABLE, "ok") if ok else (HealthStatus.UNAVAILABLE, "corrupt")

        async def vault_check() -> tuple[HealthStatus, str]:
            p = Path(self.config.paths.vault_dir)
            return (
                (HealthStatus.AVAILABLE, str(p))
                if p.exists()
                else (HealthStatus.UNAVAILABLE, "missing")
            )

        async def voice_check() -> tuple[HealthStatus, str]:
            if not self.voice_enabled:
                return HealthStatus.UNAVAILABLE, "disabled"
            w = self._workers.get("voice")
            if w is None or (w.process is not None and w.process.poll() is not None):
                return HealthStatus.UNAVAILABLE, "worker not running"
            return (
                (HealthStatus.AVAILABLE, "worker registered")
                if w.registered.is_set()
                else (HealthStatus.LIMITED, "worker starting")
            )

        checks = [
            Check("db", db_check),
            Check("vault", vault_check),
            Check("voice", voice_check),
        ]
        for prov in providers:

            async def probe(p: AiProvider = prov) -> tuple[HealthStatus, str]:
                info = await p.health()
                return info.status, info.reason

            checks.append(Check(f"ai.{prov.info.id}", probe, timeout_s=15.0))
        return checks

    def _health_json(self) -> dict[str, Any]:
        return {
            "version": nox.__version__,
            "level": str(self.state.get("system.level")),
            "components": {k: v.model_dump(mode="json") for k, v in self.health.current().items()},
        }

    def _state_json(self) -> dict[str, Any]:
        return self.state.snapshot()

    def _state_json_for(self, path: str | None = None) -> dict[str, Any]:
        snap = self.state.snapshot()
        if path:
            return {"path": path, "value": self.state.get(path)}
        return snap

    async def _providers_json(self) -> list[dict[str, Any]]:
        """`ai.providers` for the dashboard and `/health`.

        `DefaultRouter.providers()` probes every provider live, and a busy machine makes that
        slow: `claude_code` alone is allowed 15 s, well past the UI's 10 s request timeout, so the
        card sat on "no provider list received" while health had the very same providers listed as
        available (2026-09-15). The live probe therefore gets a short budget, and whatever it does
        not deliver in time is answered from the health service's last observation (the same
        probes, at most one interval old) rather than with nothing.
        """
        probe = self._providers_probe
        if probe is None or probe.done():
            # Shielded and kept running: a probe that misses the budget still finishes and fills
            # the router's health cache, so the next request is answered from live data.
            probe = asyncio.ensure_future(self.router.providers())
            probe.add_done_callback(_retrieve_exception)
            self._providers_probe = probe
            self._tasks.add(probe)
            probe.add_done_callback(self._tasks.discard)
        try:
            infos = await asyncio.wait_for(asyncio.shield(probe), PROVIDERS_PROBE_BUDGET_S)
        except TimeoutError:
            log.warning("ai.providers_probe_slow", budget_s=PROVIDERS_PROBE_BUDGET_S)
            infos = [self._provider_info_from_health(p) for p in self.ai_providers]
        except Exception as exc:  # noqa: BLE001 - a broken probe must still answer the UI
            log.warning("ai.providers_probe_failed", error=f"{type(exc).__name__}: {exc}")
            infos = [self._provider_info_from_health(p) for p in self.ai_providers]
        return [info.model_dump(mode="json") for info in infos]

    def _provider_info_from_health(self, provider: AiProvider) -> ProviderInfo:
        """The provider's static info plus the last `ai.<id>` health result, or its own status."""
        entry = self.health.current().get(f"ai.{provider.info.id}")
        if entry is None:
            return provider.info
        return provider.info.model_copy(update={"status": entry.status, "reason": entry.reason})

    # -- workers -----------------------------------------------------------------------------------
    async def _on_client_disconnected(self, ev: Event) -> None:
        """A worker whose hub connection closed is unavailable until it registers again; without
        this the core kept routing `voice.ptt`/`tts.say` to a dead client id (2026-09-15)."""
        client_id = str(ev.payload.get("client_id", ""))
        for w in self._workers.values():
            if w.client_id == client_id:
                w.client_id = None
                w.registered.clear()
                log.warning("worker.disconnected", service=w.service, client=client_id)
                self._spawn_task(self.health.run_once())

    def _worker_client(self, service: str) -> str | None:
        w = self._workers.get(service)
        return w.client_id if w and w.registered.is_set() else None

    def _spawn_worker(self, service: str) -> None:
        env = dict(os.environ)
        env.update(self.tokens.worker_env(f"worker:{service}"))
        env["NOX_HUB_URL"] = self.hub.url
        env["NOX_DATA_DIR"] = str(self.config.paths.data_dir)  # models live below it
        cmd = [*self.worker_command, "--service", service]
        try:
            proc = subprocess.Popen(cmd, env=env, cwd=str(REPO_ROOT))  # noqa: S603
        except OSError as exc:
            log.error("worker.spawn_failed", service=service, error=str(exc))
            return
        self._job.assign(proc.pid)
        self._workers[service] = WorkerProcess(service=service, process=proc)
        log.info("worker.spawned", service=service, pid=proc.pid)

    async def _terminate_workers(self) -> None:
        for w in list(self._workers.values()):
            if w.process is not None and w.process.poll() is None:
                w.process.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(w.process.wait), timeout=2.0)
                except TimeoutError:
                    w.process.kill()
            w.registered.clear()
            w.client_id = None
        self._workers.clear()

    async def _stop_voice_output(self) -> None:
        await self.speaker.interrupt(reason="kill_switch")

    async def _greet_when_voice_ready(self) -> None:
        w = self._workers.get("voice")
        if w is None:
            return
        try:
            await asyncio.wait_for(w.registered.wait(), timeout=90.0)
        except TimeoutError:
            log.warning("voice.worker_not_ready", note="no spoken greeting")
            return
        allowed, reason = self.speech_policy.may_speak("greeting")
        if not allowed:
            log.info("voice.greeting_suppressed", reason=reason)
            # Silent path: just settle into an idle expression, no TTS.
            await self.pet.set_functional(PetFunctional.IDLE, reason="ready-silent")
            return
        lang = self.orchestrator.config.default_language
        await self.speaker.say(
            TtsRequest(
                utterance_id=f"greeting:{uuid.uuid4().hex[:8]}",
                text=GREETING.get(lang, GREETING["en"]),
                language=lang,
                channel=self.orchestrator.config.channel,
            )
        )

    # -- kill switch -------------------------------------------------------------------------------
    async def _on_kill_phrase(self, ev: Event) -> None:
        await self.security.killswitch.engage("voice", ev.payload.get("reason", "kill phrase"))

    async def _on_kill_event(self, _: Event) -> None:
        await self.state.update("system.level", SystemLevel.SAFE_MODE.value, reason="kill_switch")

    async def _on_supervisor_kill(self, reason: str, by: str) -> None:
        """`sup.kill` in any mode but `restart` (kill switch, panic, safe mode)."""
        security = getattr(self, "security", None)
        if security is None:  # a kill during the first boot steps: nothing to stop yet
            log.warning("supervisor.kill_before_security", reason=reason, by=by)
            self._request_shutdown(reason or by)
            return
        await security.killswitch.engage("supervisor", reason or by)

    def _on_supervisor_restart(self, reason: str) -> None:
        """`sup.kill mode=restart`: the watchdog wants a fresh core, not safe mode. Shut down
        cleanly (exit 0) and let the supervisor respawn - engaging the kill switch here left Nox
        mute in safe mode with nothing restarted (the product owner's log, 2026-09-15)."""
        log.warning("core.restart_requested", reason=reason or "supervisor")
        self._request_shutdown(reason or "supervisor restart")

    def _request_shutdown(self, reason: str) -> None:
        """B-6: `sup.stop`; `_run()` waits on this to call `stop()` and exit 0."""
        log.warning("core.shutdown_requested", reason=reason or "supervisor")
        self.shutdown_requested.set()

    def _supervisor_status(self) -> dict[str, Any]:
        """Heartbeat decoration. Runs from the very first heartbeat, which the core now sends
        before the state manager exists, so every field is optional."""
        state = getattr(self, "state", None)
        return {"level": str(state.get("system.level")) if state is not None else "starting"}

    # -- IPC request handlers ----------------------------------------------------------------------
    def _register_handlers(self) -> None:
        reg = self.registry.register
        ui = ("shell", "dashboard")
        reg(
            "state.get",
            StateGet,
            self._h_state_get,
            roles=("shell", "dashboard", "pet", "plugin"),
        )
        reg("health.get", EmptyPayload, self._h_health_get, roles=ui)
        reg("mode.set", ModeSet, self._h_mode_set, roles=ui)
        reg("privacy.set", PrivacySet, self._h_privacy_set, roles=(*ui, "supervisor"))
        reg("security.kill", SecurityKill, self._h_kill, roles=(*ui, "supervisor"))
        reg("security.panic", SecurityPanic, self._h_panic, roles=(*ui, "supervisor"))
        reg("security.resume", SecurityResume, self._h_resume, roles=(*ui, "supervisor"))
        reg(
            "security.permission.reply", PermissionReply, self._h_permission_reply, roles=("shell",)
        )
        reg("voice.ptt", VoicePtt, self._h_voice_ptt, roles=("shell",))
        reg("voice.mute", VoiceMute, self._h_voice_mute, roles=ui)
        reg("chat.send", ChatSend, self._h_chat_send, roles=ui)
        reg("ai.providers", EmptyPayload, self._h_ai_providers, roles=ui)
        reg("pet.interact", PetInteract, self._h_pet_interact, roles=("pet", "shell"))
        reg("worker.register", WorkerRegister, self._h_worker_register, roles=("worker", "plugin"))
        reg("worker.ready", WorkerReady, self._h_worker_ready, roles=("worker", "plugin"))
        reg(
            "worker.heartbeat",
            WorkerHeartbeat,
            self._h_worker_heartbeat,
            roles=("worker", "plugin"),
        )
        reg("stream.session.status", EmptyPayload, self._h_stream_session_status, roles=ui)
        reg("stream.funken.top", FunkenTopRequest, self._h_stream_funken_top, roles=ui)

    async def _h_state_get(self, ctx: RequestContext, p: StateGet) -> dict[str, Any]:
        snap = self.state.snapshot()
        # pet sees only what it renders; a plugin only the non-private subtree (Plugin API)
        if ctx.role in ("pet", "plugin"):
            public = {
                "assistant": snap["assistant"],
                "privacy": snap["privacy"],
                "system": snap["system"],
            }
            if ctx.role == "plugin" and p.path:
                root = p.path.split(".", 1)[0]
                if root not in public:
                    raise IpcError(ERR_PERMISSION, f"plugins may not read {p.path!r}")
                return {"path": p.path, "value": self.state.get(p.path)}
            return public
        if p.path:
            return {"path": p.path, "value": self.state.get(p.path)}
        return snap

    async def _h_health_get(self, ctx: RequestContext, _: EmptyPayload) -> dict[str, Any]:
        return self._health_json()

    async def _h_mode_set(self, ctx: RequestContext, p: ModeSet) -> dict[str, Any]:
        previous = str(self.state.get("assistant.mode"))
        await self.state.update("assistant.mode", p.mode.value, reason=f"mode.set by {ctx.role}")
        profile_for_mode = {
            Mode.CODING: "coding",
            Mode.STREAM: "stream",
            Mode.RESEARCH: "research",
        }
        profile = profile_for_mode.get(p.mode, "companion")
        with contextlib.suppress(Exception):
            self.security.engine.set_profile(profile, by=ctx.role)
        await self.bus.publish(
            Event(
                name=E.SYSTEM_MODE_CHANGED,
                payload={"previous": previous, "current": p.mode.value, "reason": ctx.role},
            )
        )
        return {"ok": True, "mode": p.mode.value, "profile": profile}

    async def _h_privacy_set(self, ctx: RequestContext, p: PrivacySet) -> dict[str, Any]:
        priv = self.security.privacy
        if p.mode is not None:
            await priv.set_mode(p.mode, by=ctx.role, confirmed=p.confirmed)
        if any(v is not None for v in (p.microphone, p.screen, p.camera)):
            await priv.set_capture(
                microphone=p.microphone, screen=p.screen, camera=p.camera, by=ctx.role
            )
        await self.state.update("privacy.mode", priv.mode.value, reason="privacy.set")
        result: dict[str, Any] = priv.state.model_dump(mode="json")
        return result

    async def _h_kill(self, ctx: RequestContext, p: SecurityKill) -> dict[str, Any]:
        # A UI client may only name user origins; anything else (e.g. "tamper") is clamped to the
        # caller's role so a client cannot turn its own kill into a PIN-gated security-path kill.
        origin = p.origin if p.origin in USER_KILL_ORIGINS else ctx.role
        report = await self.security.killswitch.engage(origin, p.reason)
        return {"ok": True, "report": report.model_dump(mode="json")}

    async def _h_panic(self, ctx: RequestContext, p: SecurityPanic) -> dict[str, Any]:
        report = await self.security.killswitch.panic(by=p.origin or ctx.role)
        return {"ok": True, "report": report.model_dump(mode="json")}

    async def _h_resume(self, ctx: RequestContext, p: SecurityResume) -> dict[str, Any]:
        # OP-6 D: resume is accepted only from the user-controlled paths (tray/hotkey via the
        # supervisor, shell, dashboard) - never from `pet`, `worker` or a plugin. The PIN is
        # required only after a security-path kill (tamper, audit-chain break, panic); without a
        # PIN configured (fresh install) the explicit request counts and is audited as such.
        if ctx.role not in RESUME_ROLES:
            raise IpcError(ERR_PERMISSION, f"role {ctx.role!r} may not resume from safe mode")
        killswitch = self.security.killswitch
        pin = self.security.pin
        if killswitch.security_path and pin.is_set():
            pin_ok = bool(p.pin) and pin.verify_pin(p.pin or "", by=ctx.role).ok
        else:
            pin_ok = True
        ok = await killswitch.resume(pin_ok=pin_ok, by=ctx.role)
        if ok:
            await self.state.update("system.level", SystemLevel.RUNNING.value, reason="resume")
            await self.bus.publish(Event(name=E.SYSTEM_STARTED, payload={"resumed": True}))
            if self.voice_enabled and "voice" not in self._workers:
                self._spawn_worker("voice")
        return {"ok": ok}

    async def _h_permission_reply(self, ctx: RequestContext, p: PermissionReply) -> dict[str, Any]:
        ok = self.security.engine.reply(p.grant_id, p.decision, remember=p.remember, by="user")
        return {"ok": ok}

    async def _h_voice_ptt(self, ctx: RequestContext, p: VoicePtt) -> dict[str, Any]:
        cid = self._worker_client("voice")
        if cid is None:
            return {"ok": False, "reason": "voice worker unavailable"}
        await self.hub.request(cid, "voice.ptt", {"pressed": p.pressed}, timeout=2.0)
        return {"ok": True}

    async def _h_voice_mute(self, ctx: RequestContext, p: VoiceMute) -> dict[str, Any]:
        await self.state.update("assistant.muted", p.muted, reason="voice.mute")
        cid = self._worker_client("voice")
        if cid is not None:
            with contextlib.suppress(IpcError):
                await self.hub.request(cid, "voice.mute", {"muted": p.muted}, timeout=2.0)
        else:
            await self.bus.publish(Event(name=E.VOICE_MUTED, payload={"muted": p.muted}))
        return {"ok": True, "muted": p.muted}

    async def _h_chat_send(self, ctx: RequestContext, p: ChatSend) -> dict[str, Any]:
        async def on_chunk(delta: str) -> None:
            await ctx.stream({"delta": delta}, False)

        turn = await self.orchestrator.handle_text(
            p.text, language=p.language, speak=p.speak, on_chunk=on_chunk
        )
        return {
            "request_id": turn.request_id,
            "text": turn.response,
            "provider": turn.provider,
            "degraded": turn.degraded,
        }

    async def _h_ai_providers(self, ctx: RequestContext, _: EmptyPayload) -> dict[str, Any]:
        return {"providers": await self._providers_json()}

    async def _h_pet_interact(self, ctx: RequestContext, p: PetInteract) -> dict[str, Any]:
        await self.bus.publish(
            Event(name=E.PET_INTERACTION, payload=p.model_dump(), source=ctx.client_id)
        )
        return {"ok": True}

    async def _h_worker_register(self, ctx: RequestContext, p: WorkerRegister) -> dict[str, Any]:
        services = (
            {p.service, "voice", "tts", "stt"}
            if p.service in ("voice", "stt", "tts")
            else {p.service}
        )
        self.hub.declare_services(ctx.client_id, services)
        w = self._workers.get(p.service)
        if w is None:
            w = WorkerProcess(service=p.service, process=None)
            self._workers[p.service] = w
        w.client_id = ctx.client_id
        # `registered` (health: AVAILABLE) is only set by `worker.ready` (B-8): between register
        # and ready the worker is still loading its engines, so health reports it as LIMITED.
        log.info("worker.registered", service=p.service, client=ctx.client_id, pid=p.pid)
        self._spawn_task(self.health.run_once())
        return {"ok": True, "config": self.config.voice.model_dump(mode="json")}

    async def _h_worker_ready(self, ctx: RequestContext, p: WorkerReady) -> dict[str, Any]:
        w = self._workers.get(p.service)
        if w is None:
            w = WorkerProcess(service=p.service, process=None)
            self._workers[p.service] = w
        w.client_id = ctx.client_id
        w.registered.set()
        log.info("worker.ready", service=p.service, client=ctx.client_id)
        self._spawn_task(self.health.run_once())
        return {"ok": True}

    async def _h_worker_heartbeat(self, ctx: RequestContext, p: WorkerHeartbeat) -> dict[str, Any]:
        return {"ok": True}

    async def _h_stream_session_status(
        self, ctx: RequestContext, _: EmptyPayload
    ) -> dict[str, Any]:
        return self.stream_sessions.status()

    async def _h_stream_funken_top(
        self, ctx: RequestContext, p: FunkenTopRequest
    ) -> dict[str, Any]:
        return {"viewers": self.funken_booking.top(p.limit)}


# ---- entry points ------------------------------------------------------------------------------


def build_config(profile: str | None, user_config: Path | None) -> NoxConfig:
    user = user_config
    if user is None:
        env_user = os.environ.get("NOX_USER_CONFIG")
        if env_user:
            user = Path(env_user)
        else:
            appdata = os.environ.get("APPDATA")
            candidate = Path(appdata) / "Nox" / "user.yaml" if appdata else None
            user = candidate if candidate and candidate.exists() else None
    defaults = Path(os.environ.get("NOX_CONFIG_DEFAULTS", DEFAULTS_PATH))
    return load_config(defaults, user, profile)


async def _run(core: NoxCore) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _request_stop(*_: Any) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, _request_stop)
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, _request_stop)
    try:
        await core.start()
    except Exception as exc:
        log.error("core.boot_failed", error=str(exc), type=type(exc).__name__)
        with contextlib.suppress(Exception):
            await core.stop()
        return 1
    try:
        # B-6: exit on whichever comes first - an OS signal, or the supervisor's sup.stop.
        stop_task = asyncio.ensure_future(stop.wait())
        shutdown_task = asyncio.ensure_future(core.shutdown_requested.wait())
        try:
            await asyncio.wait({stop_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (stop_task, shutdown_task):
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
    finally:
        await core.stop()
    return 0


async def run_core(
    *, profile: str | None = None, voice: bool = True, user_config: Path | None = None
) -> int:
    try:
        config = build_config(profile, user_config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    return await _run(NoxCore(config, voice=voice))


async def run_dev(*, voice: bool = True, shell: bool = True, profile: str | None = None) -> int:
    config = build_config(profile, None)
    core = NoxCore(config, voice=voice)
    shell_proc: subprocess.Popen[bytes] | None = None
    if shell:
        env = dict(os.environ)
        env["NOX_RUNTIME_DIR"] = str(config.paths.runtime_dir)
        shell_proc = subprocess.Popen(
            [sys.executable, "-m", "nox.shell"], env=env, cwd=str(REPO_ROOT)
        )  # noqa: S603
    try:
        return await _run(core)
    finally:
        if shell_proc is not None and shell_proc.poll() is None:
            shell_proc.terminate()


def store_python_note() -> str | None:
    """Microsoft-Store Python (MSIX) virtualizes `%APPDATA%` writes into its package LocalCache;
    tools outside the package (`icacls`, Explorer, an editor) do not see those files. Nox works,
    but config and tokens are not where the docs say (observed 2026-09-15: `token_acl_failed`)."""
    if sys.platform != "win32" or "WindowsApps" not in sys.base_prefix:
        return None
    return (
        "python from the Microsoft Store: %APPDATA% writes are virtualized into the package "
        "LocalCache, so the config/token paths above are not their real location. Install "
        "python.org Python and recreate .venv to avoid this."
    )


async def run_doctor() -> int:
    """Environment report without starting servers: config, db, providers, vault, voice extras."""
    print(f"nox {nox.__version__} on {sys.platform}, python {sys.version.split()[0]}")  # noqa: T201
    try:
        config = build_config(None, None)
    except ConfigError as exc:
        print(f"[FAIL] config: {exc}")  # noqa: T201
        return 1
    print(f"[ ok ] config loaded (profile={config.profile_id or config.security.profile})")  # noqa: T201
    if (store_note := store_python_note()) is not None:
        print(f"[warn] {store_note}")  # noqa: T201
    for w in config.warnings:
        print(f"[warn] {w.model_dump()}")  # noqa: T201
    for name, p in (("vault", config.paths.vault_dir), ("database_dir", config.paths.database_dir)):
        print(f"[{' ok ' if Path(p).exists() else 'warn'}] {name}: {p}")  # noqa: T201
    ai_cfg = AiConfig.from_mapping(config.ai.model_dump())
    for prov in (
        RulesProvider(),
        OllamaProvider(ai_cfg.providers.ollama),
        ClaudeCodeProvider(ai_cfg.providers.claude_code),
    ):
        info = await prov.health()
        mark = " ok " if info.status is HealthStatus.AVAILABLE else "warn"
        print(f"[{mark}] ai.{info.id}: {info.status.value} ({info.reason})")
    for mod in ("faster_whisper", "sounddevice", "piper", "PySide6"):
        try:
            __import__(mod)
            print(f"[ ok ] {mod} importable")  # noqa: T201
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {mod}: {exc}")  # noqa: T201
    return 0


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m nox.app")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--user-config", default=None)
    args = ap.parse_args()
    code = asyncio.run(
        run_core(
            profile=args.profile,
            voice=not args.no_voice,
            user_config=Path(args.user_config) if args.user_config else None,
        )
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
