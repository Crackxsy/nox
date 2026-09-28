"""The composition root of the core process.

`NoxCore` owns the components and nothing else: it constructs them in order, connects them, and
shuts them down again. Every decision lives in the component that makes it. The boot steps
themselves are in `nox.core.boot`, one module per area, and each is a named method here - so the
order of the boot is readable in `start()` without scrolling through the twelve things it does.

The order is deliberate:

1. directories and logging, so everything after this is observable,
2. the supervisor client, before anything slow - the heartbeat has to exist, or the watchdog
   counts the boot itself as a hang,
3. the database, in a worker thread: migrations and the vector extension take seconds on a cold
   disk and are synchronous by design,
4. bus and state, then security on top of them,
5. the IPC hub and the HTTP server, so a UI can attach while the rest still comes up,
6. tools, the plugin manager, the language models, health, the assistant and the stream services,
7. the voice worker, the extensions, and finally the plugins - a plugin sees a fully booted core.

During shutdown a component that fails is logged with its name, and a component that was never
built is skipped. The two used to be indistinguishable, because every attribute only existed once
`start()` had got far enough to create it.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Awaitable
from datetime import datetime
from pathlib import Path
from typing import Any

import nox
from nox.ai.base import AiProvider, ProviderInfo
from nox.ai.config import AiConfig
from nox.ai.prompting import DECIDED_PERSONALITY_BLOCK, build_system_prompt
from nox.ai.router import DefaultRouter
from nox.core.boot.ai import ProviderCard, build_providers, build_router
from nox.core.boot.extensions import DEFAULT_EXTENSIONS, install_extensions, stop_extensions
from nox.core.boot.health import DatabaseProbe, core_health_checks
from nox.core.boot.persistence import DbTurnStore, OpenedDatabase, open_database
from nox.core.boot.retention import RetentionService, retention_rules
from nox.core.boot.voice_health import VoiceHealthReport
from nox.core.boot.workers import WorkerProcess, WorkerSpeaker, WorkerSupervisor
from nox.core.bus import AsyncEventBus
from nox.core.config import NoxConfig
from nox.core.events import E, Event
from nox.core.extension import ExtensionRuntime
from nox.core.health import Check, HealthService
from nox.core.jobobject import JobObject
from nox.core.logging import configure_logging, get_logger, shutdown_logging
from nox.core.orchestrator import Orchestrator, OrchestratorConfig
from nox.core.speech_policy import SpeechPolicy
from nox.core.state import PetFunctional, SystemLevel
from nox.core.statemgr import NoxStateManager
from nox.data.db import Database
from nox.data.repos import (
    HealthHistoryRepository,
    SessionRepository,
    StateCheckpointRepository,
    TurnRepository,
)
from nox.data.security_repos import SecurityStateRepository
from nox.data.stream_repos import (
    ChatEventRepository,
    FunkenLedgerRepository,
    StreamSessionRepository,
    ViewerRepository,
)
from nox.ipc.dispatch import RequestRegistry
from nox.ipc.handlers.core import register_core_handlers
from nox.ipc.http import HttpServer, HttpSettings, create_app
from nox.ipc.origin import ui_origins
from nox.ipc.server import HubSettings, IpcHub
from nox.ipc.tokens import TokenStore
from nox.paths import DASHBOARD_DIST, DEFAULTS_PATH, PET_DIST, PLUGINS_DIR, PROFILES_DIR, REPO_ROOT
from nox.pet.service import PetService
from nox.plugins.manager import PluginManager, PluginManagerSettings
from nox.security.killswitch import KillReport
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

#: Re-exported: these are the defaults `NoxCore` is constructed with, and a caller that builds a
#: core - the entry points, the tests - takes them from here rather than recomputing the layout.
__all__ = [
    "DASHBOARD_DIST",
    "DEFAULTS_PATH",
    "GREETING",
    "PET_DIST",
    "PLUGINS_DIR",
    "PROFILES_DIR",
    "REPO_ROOT",
    "NoxCore",
    "WorkerProcess",
]

GREETING = {"de": "Hallo, ich bin Nox. Ich bin bereit.", "en": "Hi, I am Nox. I am ready."}

#: How long the greeting waits for the voice worker to finish loading its engines before it gives
#: up and settles into a silent idle expression instead.
VOICE_READY_TIMEOUT_S = 90.0


class NoxCore:
    """Everything one running Nox consists of."""

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
        #: A test that installs extensions itself passes False.
        self.auto_extensions = extensions
        self.session_id = uuid.uuid4().hex
        self.stopped = asyncio.Event()
        #: Set by the supervisor's stop request. The entry point waits on it as well as on an OS
        #: signal, and calls `stop()` for whichever arrives first.
        self.shutdown_requested = asyncio.Event()
        #: What the voice worker last said about its own health (heartbeat), for `voice_check`.
        self.voice_report = VoiceHealthReport()

        # Components, in build order. Declared here so a half-booted core holds `None` rather than
        # a missing attribute - that is what `stop()` and the supervisor callbacks read.
        self.supervisor: SupervisorClient | None = None
        self.db: Database | None = None
        #: What opening the database took: a damaged file set aside, a backup written.
        self.db_opened: OpenedDatabase | None = None
        self.bus: AsyncEventBus | None = None
        self.state: NoxStateManager | None = None
        self.security: SecurityContext | None = None
        self.tokens: TokenStore | None = None
        self.registry: RequestRegistry | None = None
        self.hub: IpcHub | None = None
        self.http: HttpServer | None = None
        self.tool_registry: ToolRegistry | None = None
        self.tool_executor: ToolExecutor | None = None
        self.plugins: PluginManager | None = None
        self.ai_providers: list[AiProvider] = []
        self.router: DefaultRouter | None = None
        self.provider_card: ProviderCard | None = None
        self.health: HealthService | None = None
        self.retention: RetentionService | None = None
        self.pet: PetService | None = None
        self.speech_policy: SpeechPolicy | None = None
        self.sessions: SessionRepository | None = None
        self.speaker: WorkerSpeaker | None = None
        #: The conversation store: the orchestrator records into it, `chat.history` reads from it.
        self.turn_store: DbTurnStore | None = None
        self.orchestrator: Orchestrator | None = None
        self.stream_sessions: StreamSessionService | None = None
        self.funken: FunkenService | None = None
        self.funken_booking: FunkenBooking | None = None
        self.stream_responder: StreamResponder | None = None
        self.extensions: dict[str, ExtensionRuntime] = {}

        self.job = JobObject("nox-core-workers")
        self.workers = WorkerSupervisor(
            job=self.job,
            command=self.worker_command,
            cwd=REPO_ROOT,
            hub_url=lambda: self.hub.url if self.hub is not None else "",
            data_dir=Path(config.paths.data_dir),
        )
        self._started = False
        #: True unless the audit writer was still running when shutdown drained it.
        self._audit_drained = True
        self._tasks: set[asyncio.Task[Any]] = set()

    # ---- boot ------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Build every component, in order. See the module docstring for why this order."""
        self._prepare_filesystem_and_logging()
        self._build_supervisor_client()
        await asyncio.sleep(0)  # let the first heartbeat go out before the heavy steps
        await self._open_database()
        self._build_bus_and_state()
        await self._restore_state()
        await self._build_security()
        await self._build_ipc()
        self._build_tools_and_plugins()
        self._build_models()
        await self._build_health()
        await self._build_assistant_services()
        self._register_kill_switch_hooks()
        self._build_stream_services()
        self._start_voice_worker()
        await self._install_extensions()
        await self._announce_started()
        await self._start_plugins()

    def _prepare_filesystem_and_logging(self) -> None:
        paths = self.config.paths
        for directory in (paths.runtime_dir, paths.logs_dir, paths.database_dir, paths.data_dir):
            Path(directory).mkdir(parents=True, exist_ok=True)
        configure_logging(
            Path(paths.logs_dir),
            level=self.config.logging.level,
            json_file=self.config.logging.json_output,
            pii=self.config.logging.pii_filter,
            retention_days=self.config.log_retention_days,
        )
        for warning in self.config.warnings:
            log.warning("config.layer_rejected", **warning.model_dump())
        log.info(
            "core.boot",
            version=nox.__version__,
            profile=self.config.profile_id or self.config.security.profile,
        )

    def _build_supervisor_client(self) -> None:
        """Connect to the watchdog first.

        Every step below can take seconds on a cold start, and the heartbeat has to be running
        before them, or the watchdog counts the boot as a hang.
        """
        self.supervisor = SupervisorClient.from_env(
            on_kill=self._on_supervisor_kill,
            on_stop=self._request_shutdown,
            on_restart=self._on_supervisor_restart,
            status_provider=self._supervisor_status,
            interval_s=self.config.supervisor.heartbeat_interval_s,
        )
        if self.supervisor is None:
            log.warning("supervisor.absent", note="running standalone (dev mode)")
            return
        self.supervisor.start()

    async def _open_database(self) -> None:
        paths = self.config.paths
        self.db_opened = await asyncio.to_thread(
            open_database,
            Path(paths.database_dir) / "nox.db",
            backups_dir=Path(paths.backups_dir),
        )
        self.db = self.db_opened.db
        await asyncio.to_thread(self.db.load_sqlite_vec)

    def _build_bus_and_state(self) -> None:
        assert self.db is not None
        self.bus = AsyncEventBus()
        self.state = NoxStateManager(
            self.bus,
            StateCheckpointRepository(self.db),
            checkpoint_interval_s=self.config.health.checkpoint_interval_s,
        )

    async def _restore_state(self) -> None:
        assert self.state is not None
        restored = await self.state.restore_latest()
        log.info("state.restored" if restored else "state.fresh")

    async def _build_security(self) -> None:
        """Build the security core, and verify the audit chain before anything can act.

        The privacy mode, panic and the kill switch come back as they were when the core went
        down (`SecurityContext.build` restores them from the database), so a crash or a watchdog
        restart never makes Nox less private, and a kill switch that was engaged stays engaged
        until someone resumes it the normal way.

        A broken chain - a row that does not link, rows missing from the end, a database that was
        replaced - puts Nox into safe mode rather than stopping the boot. Refusing to start would
        leave the user with no UI at all: no way to read the reason and no way to resume. Safe
        mode denies every action with a side effect, keeps the audit entry, needs the PIN to
        leave, and says on screen what happened. Carrying on as normal is the one option that is
        not available.
        """
        assert self.db is not None and self.bus is not None and self.state is not None
        await asyncio.sleep(0)
        self.security = SecurityContext.build(
            self.config,
            conn=self.db.connection,
            db_lock=self.db.lock,
            profiles_dir=self.profiles_dir,
            bus=self.bus,
            session_id=self.session_id,
            state_store=SecurityStateRepository(self.db),
            audit_anchor_dir=Path(self.config.paths.runtime_dir),
            database_path=self.db.path,
        )
        verification = await asyncio.to_thread(self.security.verify_boot)
        self._audit_database_recovery()
        await self.state.update("privacy.mode", self.security.privacy.mode.value, reason="boot")
        await self.state.update("privacy.panic", self.security.privacy.panic, reason="boot")
        if not verification.ok:
            log.critical(
                "audit.chain_broken",
                first_bad_seq=verification.first_bad_seq,
                checked=verification.checked,
                reason=verification.reason,
                note="entering safe mode; leaving it needs the PIN",
            )
            await self.security.killswitch.engage(
                "audit",
                f"audit chain broken ({verification.reason}) at entry {verification.first_bad_seq}",
            )
        if self.security.killswitch.is_engaged():
            await self.state.update(
                "system.level",
                SystemLevel.SAFE_MODE.value,
                reason=f"safe_mode.{self.security.killswitch.origin}",
            )
            return
        await self.state.update("system.level", SystemLevel.RUNNING.value, reason="boot")

    def _audit_database_recovery(self) -> None:
        """A damaged database set aside at boot is the first entry of the new audit chain."""
        recovery = self.db_opened.recovery if self.db_opened is not None else None
        if recovery is None or self.security is None:
            return
        self.security.audit_store.append(
            actor="system",
            tool="core",
            action="db.recovered",
            target=recovery.moved_to.name,
            decision="allow",
            result="recovered",
            details={"reason": recovery.reason},
        )

    async def _build_ipc(self) -> None:
        assert self.bus is not None
        paths = self.config.paths
        self.tokens = TokenStore()
        self.tokens.write_session_token(Path(paths.runtime_dir))
        self.workers.use_tokens(self.tokens)
        self.registry = RequestRegistry()
        register_core_handlers(self)
        self.hub = IpcHub(
            HubSettings.from_config(self.config.ipc),
            self.tokens,
            self.registry,
            self.bus,
            Path(paths.runtime_dir),
            boundary_audit=self._audit_plugin_boundary,
        )
        await self.hub.start()
        self.http = HttpServer(
            create_app(
                health=self.health_json,
                state=self.state_json,
                providers=self.providers_json,
                session_token=lambda: self.tokens.session_token if self.tokens else "",
                dashboard_token=lambda: self.tokens.role_token("dashboard") if self.tokens else "",
                pet_dist=self.pet_dist if self.pet_dist.exists() else None,
                dashboard_dist=self.dashboard_dist if self.dashboard_dist.exists() else None,
                origin_hosts=(self.config.ipc.host,),
            ),
            HttpSettings.from_config(
                self.config.ipc, pet_dist=self.pet_dist, dashboard_dist=self.dashboard_dist
            ),
            runtime_dir=Path(paths.runtime_dir),
        )
        await self.http.start()
        # The pet and the dashboard are pages of this HTTP server; they are the only browser
        # origins the hub lets in (the Qt shell and the workers send no Origin at all).
        self.hub.allow_browser_origins(ui_origins(self.http.port, (self.config.ipc.host,)))
        log.info("ipc.ready", ws=self.hub.url, http=self.http.url)

    def _audit_plugin_boundary(self, client_id: str, what: str, name: str) -> None:
        """A plugin connection asked the hub for more than its manifest allows (`IpcHub`)."""
        if self.security is None:
            return
        self.security.audit.append(
            actor=client_id,
            tool="plugin",
            action=f"boundary.{what}",
            target=name,
            decision="deny",
            result="denied",
        )

    def _build_tools_and_plugins(self) -> None:
        """One tool catalogue shared by core and plugins, and the manager that runs them.

        The plugin manager is built here, after security and IPC exist, but started last, so a
        plugin never sees a half-built core.
        """
        assert self.security is not None and self.bus is not None and self.hub is not None
        assert self.tokens is not None and self.registry is not None
        self.tool_registry = ToolRegistry()
        self.tool_executor = ToolExecutor(
            self.tool_registry,
            self.security.engine,
            self.security.audit,
            self.bus,
            self.security.killswitch,
        )
        plugins_dir = Path(self.config.plugins.dir) if self.config.plugins.dir else PLUGINS_DIR
        self.plugins = PluginManager(
            tool_registry=self.tool_registry,
            executor=self.tool_executor,
            plugins_dir=plugins_dir,
            bus=self.bus,
            hub=self.hub,
            tokens=self.tokens,
            registry=self.registry,
            engine=self.security.engine,
            secrets=self.security.secrets,
            audit=self.security.audit,
            job=self.job,
            settings=PluginManagerSettings(enabled=list(self.config.plugins.enabled)),
            worker_command=self.worker_command,
            cwd=REPO_ROOT,
            mode=self._current_mode,
            safe_mode=self.security.killswitch.is_engaged,
            connect_state=self.security.connect_state,
            global_egress_allowlist=tuple(self.config.security.egress_allowlist),
            loopback_allowlist=tuple(self.config.security.loopback_allowlist),
            start_policy=self.security.policy,
        )
        self.plugins.register_handlers()

    def _build_models(self) -> None:
        """Every language-model client goes through the egress guard; see `nox.core.boot.ai`."""
        assert self.security is not None and self.bus is not None
        ai_config = AiConfig.from_mapping(self.config.ai.model_dump())
        # The effective policy - profile and privacy together - decides which providers may
        # answer; passing only the privacy half once sent `work` conversations to the cloud.
        self.ai_providers = build_providers(
            ai_config,
            egress=self.security.egress,
            status_source=self._health_state,
            policy=self.security.policy,
        )
        self.router = build_router(
            self.ai_providers, ai_config, bus=self.bus, policy=self.security.policy
        )
        self.provider_card = ProviderCard(
            self.ai_providers, probe=self._probe_providers, health_entry=self._health_entry
        )

    async def _build_health(self) -> None:
        assert self.db is not None and self.bus is not None and self.state is not None
        assert self.tool_registry is not None
        self.health = HealthService(
            self.bus,
            HealthHistoryRepository(self.db),
            self._health_checks(),
            interval_s=self.config.health.check_interval_s,
            state_manager=self.state,
        )
        self.retention = RetentionService(
            retention_rules(
                self.db,
                self.config,
                memory=self._memory_retention,
                audit=self.security.audit if self.security is not None else None,
            ),
            audit=self.security.audit if self.security is not None else None,
        )
        self.health.add_check(Check("retention", self.retention.health))
        await self.health.run_once()
        self.health.start()
        self.retention.start()
        register_v01_tools(self.tool_registry, state=self.state, health=self.health)

    async def _build_assistant_services(self) -> None:
        assert self.bus is not None and self.state is not None and self.security is not None
        assert self.db is not None and self.hub is not None and self.router is not None
        config = self.config
        self.pet = PetService(self.bus, self.state, safe_mode=self.security.killswitch.is_engaged)
        await self.pet.start()
        self.speech_policy = SpeechPolicy(
            state=self.state,
            config=config,
            active_zone=self._active_zone,
            privacy_mode=self._privacy_mode,
        )
        self.sessions = SessionRepository(self.db)
        await asyncio.to_thread(
            self.sessions.create,
            self._current_mode(),
            self.security.privacy.mode.value,
            session_id=self.session_id,
        )
        self.speaker = WorkerSpeaker(self.hub, lambda: self.workers.client_id("voice"))
        self.turn_store = DbTurnStore(
            TurnRepository(self.db), config.privacy.retention.raw_transcripts_days or None
        )
        self.orchestrator = Orchestrator(
            bus=self.bus,
            state=self.state,
            router=self.router,
            speaker=self.speaker,
            turns=self.turn_store,
            # Profile x privacy: a `work` profile remembers nothing even in BALANCED mode.
            memory_policy=self.security.policy,
            system_prompt=self._system_prompt,
            safe_mode=self.security.killswitch.is_engaged,
            config=OrchestratorConfig(
                default_language=config.identity.ui_language,
                channel=Channel(config.voice.channels.routing),
            ),
            session_id=self.session_id,
        )
        await self.orchestrator.start()

    def _register_kill_switch_hooks(self) -> None:
        """What has to stop when the kill switch fires, below the model layer."""
        assert self.security is not None and self.bus is not None
        killswitch = self.security.killswitch
        killswitch.register_stop_hook("orchestrator", self._cancel_orchestrator)
        killswitch.register_stop_hook("voice.stop", self._stop_voice_output)
        killswitch.register_stop_hook("workers.terminate", self.workers.terminate_all)
        # Every plugin is asked to stop and is then terminated. The acknowledgement window stays
        # below the kill switch's own 2 s hook budget, so the hook always finishes inside it.
        killswitch.register_stop_hook("plugins.stop", self._stop_plugins_for_kill)
        self.bus.subscribe(E.VOICE_KILL_PHRASE, self._on_kill_phrase)
        self.bus.subscribe(E.SECURITY_KILL_SWITCH, self._on_kill_event)
        self.bus.subscribe(E.IPC_CLIENT_DISCONNECTED, self._on_client_disconnected)

    def _build_stream_services(self) -> None:
        """Session lifecycle, the channel currency and the chat responder.

        Subscribed before the plugins start, so they see every stream event from the plugins' very
        first one.
        """
        assert self.db is not None and self.bus is not None and self.security is not None
        assert self.router is not None and self.tool_executor is not None
        config = self.config
        viewers = ViewerRepository(self.db)
        self.stream_sessions = StreamSessionService(
            self.bus,
            StreamSessionRepository(self.db),
            ChatEventRepository(self.db),
            config.stream.chat,
            viewers,
        )
        self.stream_sessions.start()
        self.funken = FunkenService(
            viewers,
            FunkenLedgerRepository(self.db),
            config.stream.funken,
            bus=self.bus,
            audit=self.security.audit,
        )
        self.funken_booking = FunkenBooking(
            self.bus,
            self.funken,
            viewers,
            self.tool_executor,
            config.stream.funken,
            is_session_active=self.stream_sessions.is_active,
        )
        self.funken_booking.start()
        self.stream_responder = StreamResponder(
            self.bus,
            self.router,
            self.tool_executor,
            config.stream.relevance,
            self.security.killswitch,
            facts=self._stream_facts,
        )
        self.stream_responder.start()

    def _start_voice_worker(self) -> None:
        if not self.voice_enabled:
            return
        self.workers.use_respawn_gate(self._may_respawn_workers, self._refresh_health)
        self.workers.spawn("voice")
        self.spawn_task(self.workers.watch())

    async def _install_extensions(self) -> None:
        if not self.auto_extensions:
            return
        names = [
            name for name in DEFAULT_EXTENSIONS if name != "remote" or self.config.remote.enabled
        ]
        self.extensions = await install_extensions(self, names)

    async def _announce_started(self) -> None:
        assert self.bus is not None
        self._started = True
        await self.bus.publish(
            Event(name=E.SYSTEM_STARTED, payload={"session_id": self.session_id})
        )
        self.spawn_task(self._greet_when_voice_ready())

    async def _start_plugins(self) -> None:
        assert self.plugins is not None and self.health is not None
        await self.plugins.start()
        for check in self.plugins.health_checks():
            self.health.add_check(check)
        log.info("core.started", session_id=self.session_id)

    # ---- shutdown --------------------------------------------------------------------------------

    async def stop(self, reason: str = "shutdown") -> None:
        """Shut down in reverse build order.

        The supervisor allows six seconds for this. A component that was never built is skipped; a
        component that fails to stop is named in the log, because "was never there" and "would not
        go down" are different problems and used to look the same.
        """
        if not self._started:
            return
        self._started = False
        log.info("core.stopping", reason=reason)
        if self.state is not None:
            await self.state.update("system.level", SystemLevel.STOPPING.value, reason="stop")
        if self.bus is not None:
            await self.bus.publish(Event(name=E.SYSTEM_STOPPING))
        for task in list(self._tasks):
            task.cancel()
        if self.provider_card is not None:
            await self.provider_card.cancel()
        await stop_extensions(self.extensions)
        for name, component in self._stoppable():
            if component is None:
                continue
            try:
                await component.stop()
            except Exception as exc:  # noqa: BLE001 - one component must not block the rest
                log.warning(
                    "core.component_stop_failed",
                    component=name,
                    error=f"{type(exc).__name__}: {exc}",
                )
        await self.workers.terminate_all()
        await self._close_session(reason)
        if self.tokens is not None:
            self.tokens.remove_session_token()
        if self.db is not None:
            if self._audit_drained:
                self.db.close()
            else:
                # The audit writer thread is still inside the connection. Closing it underneath
                # that thread is a use-after-close in SQLite's C code - a crash, not an exception.
                # Leaving it open for the exiting process is safe: every committed row is on disk.
                log.error("core.database_left_open", reason="audit writer still draining")
        self.job.close()
        shutdown_logging()
        self.stopped.set()

    def _stoppable(self) -> tuple[tuple[str, Any], ...]:
        """The components with a `stop()`, in shutdown order: the reverse of how they are built."""
        return (
            ("orchestrator", self.orchestrator),
            ("stream_responder", self.stream_responder),
            ("funken_booking", self.funken_booking),
            ("stream_sessions", self.stream_sessions),
            ("pet", self.pet),
            ("retention", self.retention),
            ("health", self.health),
            ("router", self.router),
            ("plugins", self.plugins),
            ("supervisor", self.supervisor),
            ("http", self.http),
            ("hub", self.hub),
        )

    async def _close_session(self, reason: str) -> None:
        """The final checkpoint, the `system.stopped` audit entry, and the session row."""
        if self.state is not None:
            # The state manager is closed here rather than in `_stoppable`, because it needs its
            # final checkpoint written first and `close()` is what flushes the rest.
            try:
                await self.state.checkpoint(immediate=True, reason="shutdown")
                await self.state.close()
            except Exception as exc:  # noqa: BLE001 - a lost checkpoint must not block shutdown
                log.warning("core.state_close_failed", error=f"{type(exc).__name__}: {exc}")
        if self.security is not None:
            self.security.audit_store.append(
                actor="system",
                tool="core",
                action="system.stopped",
                target="",
                decision="allow",
                result="ok",
                details={"reason": reason},
            )
            self._audit_drained = self.security.close()
            if not self._audit_drained:
                log.error("audit.pending_entries_on_shutdown")
        if self.sessions is not None:
            try:
                await asyncio.to_thread(self.sessions.end, self.session_id)
            except Exception as exc:  # noqa: BLE001 - the process is going down either way
                log.warning("core.session_close_failed", error=f"{type(exc).__name__}: {exc}")

    # ---- shared helpers --------------------------------------------------------------------------

    def spawn_task(self, coro: Awaitable[Any]) -> None:
        """Run `coro` in the background, holding a reference until it completes."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def ensure_voice_worker(self) -> None:
        """Spawn the voice worker if it is enabled and not running (used when resuming).

        Earlier failures are forgotten: resuming is the user asking for a fresh attempt.
        """
        if self.voice_enabled:
            self.workers.restart_fresh("voice")

    def _may_respawn_workers(self) -> bool:
        """No worker comes back during safe mode or while the core shuts down."""
        engaged = self.security is not None and self.security.killswitch.is_engaged()
        return self._started and not engaged

    def _refresh_health(self) -> None:
        if self.health is not None:
            self.spawn_task(self.health.run_once())

    def _memory_retention(self) -> Any:
        """The memory extension's retention job, when that extension is installed."""
        memory: Any = self.extensions.get("memory")
        return memory.retention if memory is not None else None

    def _current_mode(self) -> str:
        return str(self.state.get("assistant.mode")) if self.state is not None else ""

    def _privacy_mode(self) -> str:
        return self.security.privacy.mode.value if self.security is not None else ""

    def _active_zone(self) -> str | None:
        return self.security.privacy.active_zone if self.security is not None else None

    def _health_state(self) -> dict[str, Any]:
        return dict(self.state.get("system.health")) if self.state is not None else {}

    def _health_entry(self, name: str) -> Any:
        return self.health.current().get(name) if self.health is not None else None

    async def _probe_providers(self) -> Any:
        return await self.router.providers() if self.router is not None else []

    async def _stop_plugins_for_kill(self) -> None:
        if self.plugins is not None:
            await self.plugins.stop_all("kill_switch", ack_timeout_s=1.5)

    def _system_prompt(self) -> str:
        return build_system_prompt(DECIDED_PERSONALITY_BLOCK, self._facts(mode=None))

    def _stream_facts(self) -> dict[str, object]:
        """Facts for the chat responder's prompt.

        The same shape as the assistant's, but the mode is fixed to "stream": a chat answer is
        always in the stream persona, even while the assistant itself is in another mode.
        """
        return self._facts(mode="stream")

    def _facts(self, *, mode: str | None) -> dict[str, object]:
        return {
            "mode": mode if mode is not None else self._current_mode(),
            "privacy_mode": self._privacy_mode(),
            "user": self.config.identity.user_display_name or "the user",
            "time": datetime.now().astimezone().isoformat(timespec="minutes"),
        }

    # ---- health and HTTP payloads ----------------------------------------------------------------

    def _health_checks(self) -> list[Check]:
        """The core's own checks; see `nox.core.boot.health`. Extensions add theirs on install."""
        return core_health_checks(
            database=lambda: self.db,
            vault_dir=lambda: Path(self.config.paths.vault_dir),
            workers=self.workers,
            voice_enabled=self.voice_enabled,
            tokens=lambda: self.tokens,
            providers=lambda: self.ai_providers,
            secrets=lambda: self.security.secrets if self.security is not None else None,
            database_probe=DatabaseProbe(lambda: self.db, lambda: self.db_opened),
            provider_health=self._provider_health,
            voice_report=lambda: self.voice_report,
        )

    async def _provider_health(self, provider: AiProvider) -> ProviderInfo:
        """Through the router: its cache, and no probe of a cloud provider privacy has blocked."""
        if self.router is None:
            return await provider.health()
        return await self.router.health(provider)

    def health_json(self) -> dict[str, Any]:
        """The `/health` body and the `health.get` response.

        `ws_port` is published here so a page this core serves can find the hub instead of guessing:
        the browser clients fall back to the default port when it is missing, which leaves the pet
        window silently offline on any non-default port.
        """
        components = self.health.current().items() if self.health is not None else ()
        payload: dict[str, Any] = {
            "version": nox.__version__,
            "level": str(self.state.get("system.level")) if self.state else "starting",
            "components": {k: v.model_dump(mode="json") for k, v in components},
        }
        if self.hub is not None:
            payload["ws_port"] = self.hub.port
        return payload

    def state_json(self, path: str | None = None) -> dict[str, Any]:
        """The authenticated `/api/state` body: the whole snapshot, or one path from it."""
        if self.state is None:
            return {}
        if path:
            return {"path": path, "value": self.state.get(path)}
        return self.state.snapshot()

    async def providers_json(self) -> list[dict[str, Any]]:
        """The `ai.providers` payload; see `nox.core.boot.ai.ProviderCard`."""
        if self.provider_card is None:
            return []
        return await self.provider_card.read()

    # ---- worker and voice callbacks --------------------------------------------------------------

    async def _on_client_disconnected(self, event: Event) -> None:
        client_id = str(event.payload.get("client_id", ""))
        if self.hub is not None and self.hub.find_client(client_id) is not None:
            return  # the worker has already reconnected; this is its old connection's goodbye
        detached = self.workers.detach(client_id)
        if detached is not None and detached.service == "voice":
            self.voice_report.clear()
        if detached is not None and self.health is not None:
            self.spawn_task(self.health.run_once())

    async def _cancel_orchestrator(self) -> None:
        if self.orchestrator is not None:
            await self.orchestrator.cancel(reason="kill")

    async def _stop_voice_output(self) -> None:
        if self.speaker is not None:
            await self.speaker.interrupt(reason="kill_switch")

    async def _greet_when_voice_ready(self) -> None:
        worker = self.workers.get("voice")
        if worker is None or self.speaker is None or self.orchestrator is None:
            return
        try:
            await asyncio.wait_for(worker.registered.wait(), timeout=VOICE_READY_TIMEOUT_S)
        except TimeoutError:
            log.warning("voice.worker_not_ready", note="no spoken greeting")
            return
        allowed, reason = (
            self.speech_policy.may_speak("greeting")
            if self.speech_policy is not None
            else (False, "no speech policy")
        )
        if not allowed:
            log.info("voice.greeting_suppressed", reason=reason)
            if self.pet is not None:  # the silent path: settle into idle, no speech
                await self.pet.set_functional(PetFunctional.IDLE, reason="ready-silent")
            return
        language = self.orchestrator.config.default_language
        await self.speaker.say(
            TtsRequest(
                utterance_id=f"greeting:{uuid.uuid4().hex[:8]}",
                text=GREETING.get(language, GREETING["en"]),
                language=language,
                channel=self.orchestrator.config.channel,
            )
        )

    # ---- kill switch and supervisor --------------------------------------------------------------

    async def _on_kill_phrase(self, event: Event) -> None:
        if self.security is not None:
            await self.security.killswitch.engage(
                "voice", str(event.payload.get("reason", "kill phrase"))
            )

    async def _on_kill_event(self, _: Event) -> None:
        if self.state is not None:
            await self.state.update(
                "system.level", SystemLevel.SAFE_MODE.value, reason="kill_switch"
            )

    async def panic_report(self, *, by: str) -> KillReport:
        """`security.panic`: engage the kill switch, force privacy offline and hide the pet."""
        assert self.security is not None
        return await self.security.killswitch.panic(by=by)

    async def _on_supervisor_kill(self, reason: str, by: str) -> None:
        """`sup.kill` in any mode but `restart`: the kill switch, panic, safe mode."""
        if self.security is None:  # a kill during the first boot steps: nothing to stop yet
            log.warning("supervisor.kill_before_security", reason=reason, by=by)
            self._request_shutdown(reason or by)
            return
        await self.security.killswitch.engage("supervisor", reason or by)

    async def notify_supervisor_resumed(self, *, by: str) -> str:
        """After a resume: let the supervisor leave its own safe mode and watch the core again.

        `standalone` without a supervisor (`nox dev`); otherwise what the supervisor answered.
        """
        if self.supervisor is None:
            return "standalone"
        return await self.supervisor.notify_resumed(by=by)

    def _on_supervisor_restart(self, reason: str) -> None:
        """`sup.kill mode=restart`: the watchdog wants a fresh core, not safe mode.

        Shut down cleanly and let the supervisor respawn. Engaging the kill switch here left Nox
        mute in safe mode with nothing restarted.
        """
        log.warning("core.restart_requested", reason=reason or "supervisor")
        self._request_shutdown(reason or "supervisor restart")

    def _request_shutdown(self, reason: str) -> None:
        """`sup.stop`: the entry point waits on this, calls `stop()` and exits cleanly."""
        log.warning("core.shutdown_requested", reason=reason or "supervisor")
        self.shutdown_requested.set()

    def _supervisor_status(self) -> dict[str, Any]:
        """Heartbeat decoration. The first heartbeat goes out before the state manager exists."""
        return {"level": str(self.state.get("system.level")) if self.state else "starting"}


if __name__ == "__main__":  # `python -m nox.app`
    from nox.entrypoints import main

    main()
