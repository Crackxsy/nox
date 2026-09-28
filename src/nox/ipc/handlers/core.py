"""The core's own IPC requests: state, mode, privacy, kill switch, voice, chat, pet and workers.

Both halves of a request live here - the payload model and the handler that receives it - so a
reader sees the whole contract in one place, the way the settings area already did it.
`register_core_handlers(core)` is called once during boot, before the hub accepts connections.

Two boundaries are enforced in this module rather than left to the caller:

* **A security change carries the PIN.** `privacy.set` can weaken protection, and
  `security.pin_required_for_security_changes` says a configured PIN must authorise that. Only a
  relaxing change is gated: switching *to* a stricter privacy mode, or turning a capture device
  *off*, never asks for a PIN, because nobody may be prevented from making Nox safer.
* **A worker may only register the service it was spawned as.** The core spawns a worker with a
  one-time token issued to the client id `worker:<service>`, so the id the connection
  authenticated as decides which service it may claim - not the service name in its own payload.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from nox.core.events import E, Event, HealthStatus, VoicePttRefused
from nox.core.logging import get_logger
from nox.core.state import Mode, PrivacyMode, SystemLevel
from nox.ipc.dispatch import EmptyPayload, RequestContext
from nox.ipc.errors import ERR_PERMISSION, ERR_UNAVAILABLE, IpcError
from nox.security.constants import RESUME_ROLES, USER_KILL_ORIGINS
from nox.security.gate import PinRequiredError, relaxes_privacy
from nox.security.model import Decision
from nox.security.pin_setup import pin_error_from_status
from nox.security.secrets import SecretStoreUnavailableError
from nox.security.service import connect_state_of

if TYPE_CHECKING:
    from nox.app import NoxCore

log = get_logger(__name__)

__all__ = [
    "PROFILE_FOR_MODE",
    "PUBLIC_STATE_ROOTS",
    "ChatHistoryRequest",
    "ChatSend",
    "CoreHandlers",
    "FunkenTopRequest",
    "ModeSet",
    "PermissionReply",
    "PetInteract",
    "PrivacySet",
    "PrivacyStatus",
    "SecurityKill",
    "SecurityPanic",
    "SecurityResume",
    "StateGet",
    "VoiceMute",
    "VoicePtt",
    "WorkerFailed",
    "WorkerHeartbeat",
    "WorkerReady",
    "WorkerRegister",
    "register_core_handlers",
    "without_paths",
]

#: How much of a worker's failure reason health shows.
MAX_WORKER_REASON_CHARS = 300

#: An absolute POSIX or Windows path; group 1 is its last component.
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s'\"<>|:;,\\/]+[\\/])+([^\s'\"<>|:;,\\/]*)")


def without_paths(text: str) -> str:
    """`text` with every absolute path shortened to its file name."""
    return _ABSOLUTE_PATH.sub(lambda match: match.group(1) or "<path>", text)


#: The state subtrees a pet renderer or a plugin may read. Everything else - conversations,
#: memory, stream data - is outside what either of them needs to do its job.
PUBLIC_STATE_ROOTS: tuple[str, ...] = ("assistant", "privacy", "system")

#: Assistant mode -> the permission profile it selects.
PROFILE_FOR_MODE: dict[Mode, str] = {
    Mode.CODING: "coding",
    Mode.STREAM: "stream",
    Mode.RESEARCH: "research",
}
DEFAULT_PROFILE = "companion"

UI_ROLES: tuple[str, ...] = ("shell", "dashboard")

#: The largest page `chat.history` returns at once.
CHAT_HISTORY_MAX_LIMIT = 200


# ---- payload models -----------------------------------------------------------------------------


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
    #: Required only for a change that relaxes privacy, and only while a PIN is configured.
    pin: str | None = None


class PrivacyStatus(BaseModel):
    """No fields: `privacy.status` reads the effective state, it never changes it."""


class SecurityKill(BaseModel):
    reason: str = ""
    origin: str = "ui"


class SecurityPanic(BaseModel):
    origin: str = "ui"


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


class ChatHistoryRequest(BaseModel):
    #: At most this many turns, newest first before `before`, returned oldest first.
    limit: int = Field(default=50, ge=1, le=CHAT_HISTORY_MAX_LIMIT)
    #: Only turns with an id below this one: the cursor for "load older turns".
    before: int | None = Field(default=None, ge=1)


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


class ComponentHealth(BaseModel):
    status: HealthStatus
    reason: str = ""


class WorkerHeartbeat(BaseModel):
    load: float = 0.0
    status: str = "running"
    #: The worker's own view of its components (voice: capture, wake gate, echo, stt, tts).
    health: dict[str, ComponentHealth] = {}


class WorkerFailed(BaseModel):
    """`worker.failed`: a worker says why it is about to exit (a model it could not load)."""

    service: str
    reason: str = Field(max_length=2000)


class FunkenTopRequest(BaseModel):
    limit: int = 10


# ---- registration -------------------------------------------------------------------------------


def register_core_handlers(core: NoxCore) -> None:
    """Register every core request on `core.registry`."""
    CoreHandlers(core).register()


class CoreHandlers:
    """The handlers, bound to one core. A class, so each handler stays a short method."""

    def __init__(self, core: NoxCore) -> None:
        self._core = core

    def register(self) -> None:
        assert self._core.registry is not None
        reg = self._core.registry.register
        ui = UI_ROLES
        reg("state.get", StateGet, self.state_get, roles=(*ui, "pet", "plugin"))
        reg("health.get", EmptyPayload, self.health_get, roles=ui)
        reg("mode.set", ModeSet, self.mode_set, roles=ui)
        reg("privacy.set", PrivacySet, self.privacy_set, roles=(*ui, "supervisor"))
        reg("privacy.status", PrivacyStatus, self.privacy_status, roles=ui)
        reg("security.kill", SecurityKill, self.kill, roles=(*ui, "supervisor"))
        reg("security.panic", SecurityPanic, self.panic, roles=(*ui, "supervisor"))
        reg("security.resume", SecurityResume, self.resume, roles=(*ui, "supervisor"))
        reg("security.permission.reply", PermissionReply, self.permission_reply, roles=("shell",))
        reg("voice.ptt", VoicePtt, self.voice_ptt, roles=("shell",))
        reg("voice.mute", VoiceMute, self.voice_mute, roles=ui)
        reg("chat.send", ChatSend, self.chat_send, roles=ui)
        reg("chat.history", ChatHistoryRequest, self.chat_history, roles=("dashboard",))
        reg("ai.providers", EmptyPayload, self.ai_providers, roles=ui)
        reg("pet.interact", PetInteract, self.pet_interact, roles=("pet", "shell"))
        # Worker registration is for spawned workers only; see the module docstring.
        reg("worker.register", WorkerRegister, self.worker_register, roles=("worker",))
        reg("worker.ready", WorkerReady, self.worker_ready, roles=("worker",))
        reg("worker.heartbeat", WorkerHeartbeat, self.worker_heartbeat, roles=("worker", "plugin"))
        reg("worker.failed", WorkerFailed, self.worker_failed, roles=("worker",))
        reg("plugin.status", EmptyPayload, self.plugin_status, roles=ui)
        reg("stream.session.status", EmptyPayload, self.stream_session_status, roles=ui)
        reg("stream.funken.top", FunkenTopRequest, self.stream_funken_top, roles=ui)

    # ---- state and health ------------------------------------------------------------------------

    async def state_get(self, ctx: RequestContext, p: StateGet) -> dict[str, Any]:
        core = self._core
        assert core.state is not None
        snapshot = core.state.snapshot()
        if ctx.role not in ("pet", "plugin"):
            if p.path:
                return {"path": p.path, "value": core.state.get(p.path)}
            return snapshot
        public = {root: snapshot[root] for root in PUBLIC_STATE_ROOTS}
        if ctx.role == "plugin" and p.path:
            if p.path.split(".", 1)[0] not in PUBLIC_STATE_ROOTS:
                raise IpcError(ERR_PERMISSION, f"plugins may not read {p.path!r}")
            return {"path": p.path, "value": core.state.get(p.path)}
        return public

    async def health_get(self, _ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return self._core.health_json()

    async def ai_providers(self, _ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return {"providers": await self._core.providers_json()}

    # ---- mode and privacy ------------------------------------------------------------------------

    async def mode_set(self, ctx: RequestContext, p: ModeSet) -> dict[str, Any]:
        """Switch the assistant mode and the permission profile that belongs to it.

        The profile comes from a vetted file and every mode is meant to be one click away, so this
        is not PIN-gated. It reports the profile the engine actually ended up on: telling the UI a
        profile changed when it did not is the kind of small lie that costs trust later.
        """
        core = self._core
        assert core.state is not None and core.security is not None and core.bus is not None
        previous = str(core.state.get("assistant.mode"))
        await core.state.update("assistant.mode", p.mode.value, reason=f"mode.set by {ctx.role}")
        profile = PROFILE_FOR_MODE.get(p.mode, DEFAULT_PROFILE)
        try:
            core.security.engine.set_profile(profile, by=ctx.role)
        except (KeyError, ValueError) as exc:
            log.error("security.profile_switch_failed", profile=profile, error=str(exc))
        active = core.security.engine.active_profile().id
        await core.bus.publish(
            Event(
                name=E.SYSTEM_MODE_CHANGED,
                payload={"previous": previous, "current": p.mode.value, "reason": ctx.role},
            )
        )
        return {"ok": True, "mode": p.mode.value, "profile": active}

    async def privacy_set(self, ctx: RequestContext, p: PrivacySet) -> dict[str, Any]:
        core = self._core
        assert core.security is not None and core.state is not None
        privacy = core.security.privacy
        await self._authorize_privacy_change(ctx, p)
        change = None
        if p.mode is not None:
            change = await privacy.set_mode(p.mode, by=ctx.role, confirmed=p.confirmed)
        if any(value is not None for value in (p.microphone, p.screen, p.camera)):
            await privacy.set_capture(
                microphone=p.microphone, screen=p.screen, camera=p.camera, by=ctx.role
            )
        await core.state.update("privacy.mode", privacy.mode.value, reason="privacy.set")
        result: dict[str, Any] = privacy.state.model_dump(mode="json")
        # A mode change that was not applied - FULL without `confirmed` - used to come back as the
        # unchanged state, which a UI could only read as success. It now says so.
        needs_confirmation = change is not None and change.requires_confirmation
        result["applied"] = not needs_confirmation
        result["requires_confirmation"] = needs_confirmation
        return result

    async def privacy_status(self, _ctx: RequestContext, _p: PrivacyStatus) -> dict[str, Any]:
        """The effective privacy picture a UI starts from after it (re)connects.

        Events only carry changes; a tray that connected after a zone was entered, or after Nox
        was muted, would otherwise show the defaults until the next change.
        """
        core = self._core
        assert core.security is not None and core.state is not None
        privacy = core.security.privacy
        return {
            "mode": privacy.mode.value,
            "capture": privacy.effective_capture().model_dump(mode="json"),
            "zone_active": privacy.active_zone is not None,
            "panic": privacy.panic,
            "safe_mode": core.security.killswitch.is_engaged(),
            "muted": bool(core.state.get("assistant.muted")),
        }

    async def _authorize_privacy_change(self, ctx: RequestContext, p: PrivacySet) -> None:
        """Ask for the PIN when the request would give Nox more freedom than it has now."""
        core = self._core
        assert core.security is not None
        gate = core.security.gate
        if not gate.is_required():
            return
        current = core.security.privacy.mode
        relaxing = p.mode is not None and relaxes_privacy(current, p.mode)
        enabling = any(value is True for value in (p.microphone, p.screen, p.camera))
        if not relaxing and not enabling:
            return
        try:
            await gate.require(p.pin, action="privacy.set", by=ctx.role)
        except PinRequiredError as exc:
            raise IpcError(ERR_PERMISSION, exc.reason, details=exc.details) from exc

    # ---- kill switch -----------------------------------------------------------------------------

    async def kill(self, ctx: RequestContext, p: SecurityKill) -> dict[str, Any]:
        # A UI client may only name a user origin; anything else is clamped to the caller's role,
        # so a client cannot dress its own kill up as a PIN-gated security event.
        assert self._core.security is not None
        origin = p.origin if p.origin in USER_KILL_ORIGINS else ctx.role
        report = await self._core.security.killswitch.engage(origin, p.reason)
        return {"ok": True, "report": report.model_dump(mode="json")}

    async def panic(self, ctx: RequestContext, p: SecurityPanic) -> dict[str, Any]:
        report = await self._core.panic_report(by=p.origin or ctx.role)
        return {"ok": True, "report": report.model_dump(mode="json")}

    async def resume(self, ctx: RequestContext, p: SecurityResume) -> dict[str, Any]:
        """Leave safe mode, and tell the supervisor so its watchdog restarts a crashed core again.

        Only the user-controlled surfaces may ask: the shell, the dashboard, and the tray or
        hotkey path that reaches the core through the supervisor. The PIN is required after a
        security-path kill - tamper, a broken audit chain, panic; without a PIN configured the
        explicit request is the authorisation, and it is audited as such.

        The supervisor is told only after the kill switch really let go, over the core's own
        authenticated control connection: a hotkey kill put the supervisor into safe mode too, and
        without this it stayed there - never restarting a crashed core again. A refusal says why
        (`reason`), so the dashboard can ask for the PIN instead of showing a bare "no".
        """
        core = self._core
        assert core.security is not None and core.state is not None and core.bus is not None
        if ctx.role not in RESUME_ROLES:
            raise IpcError(ERR_PERMISSION, f"role {ctx.role!r} may not resume from safe mode")
        killswitch = core.security.killswitch
        pin_ok, refusal = await self._resume_pin_check(p.pin, by=ctx.role)
        ok = await killswitch.resume(pin_ok=pin_ok, by=ctx.role)
        if not ok:
            return {"ok": False, **refusal}
        # Resuming from a broken audit chain is the acknowledgement that it was seen; without it
        # every later boot would find the same break and stop again.
        await asyncio.to_thread(core.security.acknowledge_audit_break, by=ctx.role)
        await core.state.update("system.level", SystemLevel.RUNNING.value, reason="resume")
        await core.bus.publish(Event(name=E.SYSTEM_STARTED, payload={"resumed": True}))
        core.ensure_voice_worker()
        supervisor = await core.notify_supervisor_resumed(by=ctx.role)
        return {"ok": True, "supervisor": supervisor}

    async def _resume_pin_check(self, pin: str | None, *, by: str) -> tuple[bool, dict[str, Any]]:
        """`(pin_ok, refusal)`: the PIN is checked only after a security-path kill."""
        security = self._core.security
        assert security is not None
        if not security.killswitch.security_path:
            return True, {}
        try:
            pin_set = security.pin.is_set()
        except SecretStoreUnavailableError:
            return False, {"reason": "store_unavailable"}
        if not pin_set:
            return True, {}
        if not pin:
            return False, {"reason": "pin_required"}
        status = await security.pin.verify_pin_async(pin, by=by)
        if status.ok:
            return True, {}
        return False, pin_error_from_status(status, action="security.resume").payload()

    async def permission_reply(self, _ctx: RequestContext, p: PermissionReply) -> dict[str, Any]:
        assert self._core.security is not None
        ok = self._core.security.engine.reply(
            p.grant_id, p.decision, remember=p.remember, by="user"
        )
        return {"ok": ok}

    # ---- voice, chat, pet ------------------------------------------------------------------------

    async def voice_ptt(self, _ctx: RequestContext, p: VoicePtt) -> dict[str, Any]:
        """Forward push-to-talk, or say why the microphone stays closed.

        A refused press is published as `voice.ptt_refused`, so the shell can tell the user - a
        key that silently does nothing (a privacy zone matched the window title) looks broken.
        """
        core = self._core
        assert core.hub is not None
        refusal = self._ptt_refusal() if p.pressed else None
        client_id = core.workers.client_id("voice")
        if refusal is None and client_id is None and p.pressed:
            refusal = VoicePttRefused(reason="voice_unavailable")
        if refusal is not None:
            await self._publish_ptt_refused(refusal)
            return {"ok": False, **refusal.model_dump(mode="json")}
        if client_id is None:
            return {"ok": False, "reason": "voice_unavailable"}
        await core.hub.request(client_id, "voice.ptt", {"pressed": p.pressed}, timeout=2.0)
        return {"ok": True}

    def _ptt_refusal(self) -> VoicePttRefused | None:
        """Why the microphone would not open for push-to-talk right now, or None."""
        core = self._core
        security = core.security
        if security is None:
            return VoicePttRefused(reason="voice_unavailable")
        privacy = security.privacy
        if security.killswitch.is_engaged():
            return VoicePttRefused(reason="safe_mode")
        if privacy.panic:
            return VoicePttRefused(reason="panic")
        if privacy.active_zone is not None:
            return VoicePttRefused(reason="privacy_zone", zone=privacy.active_zone)
        if not privacy.allows_capture("microphone"):
            return VoicePttRefused(reason="microphone_off")
        if core.state is not None and bool(core.state.get("assistant.muted")):
            return VoicePttRefused(reason="muted")
        return None

    async def _publish_ptt_refused(self, refusal: VoicePttRefused) -> None:
        core = self._core
        log.info("voice.ptt_refused", reason=refusal.reason, zone=refusal.zone)
        if core.bus is not None:
            await core.bus.publish(
                Event(name=E.VOICE_PTT_REFUSED, payload=refusal.model_dump(mode="json"))
            )

    async def voice_mute(self, _ctx: RequestContext, p: VoiceMute) -> dict[str, Any]:
        core = self._core
        assert core.state is not None and core.bus is not None and core.hub is not None
        await core.state.update("assistant.muted", p.muted, reason="voice.mute")
        client_id = core.workers.client_id("voice")
        if client_id is None:
            await core.bus.publish(Event(name=E.VOICE_MUTED, payload={"muted": p.muted}))
            return {"ok": True, "muted": p.muted}
        try:
            await core.hub.request(client_id, "voice.mute", {"muted": p.muted}, timeout=2.0)
        except IpcError as exc:
            # The state change stands; `worker.register` hands the flag to the worker when it
            # reconnects or restarts.
            log.warning("voice.mute_not_delivered", code=exc.code, muted=p.muted)
        return {"ok": True, "muted": p.muted}

    async def chat_send(self, ctx: RequestContext, p: ChatSend) -> dict[str, Any]:
        assert self._core.orchestrator is not None

        async def on_chunk(delta: str) -> None:
            await ctx.stream({"delta": delta}, False)

        turn = await self._core.orchestrator.handle_text(
            p.text, language=p.language, speak=p.speak, on_chunk=on_chunk
        )
        return {
            "request_id": turn.request_id,
            "text": turn.response,
            "provider": turn.provider,
            "degraded": turn.degraded,
        }

    async def chat_history(self, _ctx: RequestContext, p: ChatHistoryRequest) -> dict[str, Any]:
        """Persisted turns across sessions, oldest first, for the dashboard's Chat page.

        Only what was recorded can come back: the orchestrator records nothing while memory writes
        are not allowed (private mode, a privacy zone, safe mode), and a turn past its retention
        date is left out even before the retention job has deleted it.
        """
        store = self._core.turn_store
        if store is None:
            raise IpcError(ERR_UNAVAILABLE, "the conversation store is not running")
        turns, has_more = await store.history(p.limit, before=p.before)
        return {"turns": [turn.model_dump(mode="json") for turn in turns], "has_more": has_more}

    async def pet_interact(self, ctx: RequestContext, p: PetInteract) -> dict[str, Any]:
        assert self._core.bus is not None
        await self._core.bus.publish(
            Event(name=E.PET_INTERACTION, payload=p.model_dump(), source=ctx.client_id)
        )
        return {"ok": True}

    # ---- workers ---------------------------------------------------------------------------------

    async def worker_register(self, ctx: RequestContext, p: WorkerRegister) -> dict[str, Any]:
        """Bind a spawned worker's connection to the service it was spawned for.

        Which service that is comes from the client id the connection authenticated as, not from
        the payload: the core issued that id together with a one-time token, so it is the only
        part of this request the core itself decided.
        """
        core = self._core
        assert core.hub is not None and core.health is not None
        expected = f"worker:{p.service}"
        if ctx.client_id != expected:
            log.warning(
                "worker.register_rejected", client=ctx.client_id, service=p.service, role=ctx.role
            )
            raise IpcError(
                ERR_PERMISSION,
                f"client {ctx.client_id!r} was not spawned as the {p.service!r} worker",
            )
        services = (
            {p.service, "voice", "tts", "stt"}
            if p.service in ("voice", "stt", "tts")
            else {p.service}
        )
        core.hub.declare_services(ctx.client_id, services)
        core.workers.attach(p.service, ctx.client_id)
        # `registered` - and with it health AVAILABLE - is set by `worker.ready`, not here: between
        # register and ready the worker is still loading its engines, and health says `limited`.
        log.info("worker.registered", service=p.service, client=ctx.client_id, pid=p.pid)
        core.spawn_task(core.health.run_once())
        # The worker's starting point for its capture gate. Events only carry changes: a privacy
        # zone entered, a microphone switched off or a kill engaged before this worker connected
        # would otherwise never reach it, and it would open the microphone anyway.
        return {
            "ok": True,
            "config": core.config.voice.model_dump(mode="json"),
            **connect_state_of(core.security),
            # The core owns the mute flag; a restarted worker must not come back unmuted.
            "muted": bool(core.state.get("assistant.muted")) if core.state is not None else False,
        }

    async def worker_ready(self, ctx: RequestContext, p: WorkerReady) -> dict[str, Any]:
        core = self._core
        assert core.health is not None
        worker = core.workers.get(p.service)
        if worker is None or worker.client_id != ctx.client_id:
            raise IpcError(ERR_UNAVAILABLE, f"{p.service!r} has not registered on this connection")
        worker.registered.set()
        log.info("worker.ready", service=p.service, client=ctx.client_id)
        core.spawn_task(core.health.run_once())
        return {"ok": True}

    async def worker_heartbeat(self, ctx: RequestContext, p: WorkerHeartbeat) -> dict[str, Any]:
        """Liveness, plus the voice worker's own health report for the `voice` check."""
        core = self._core
        voice = core.workers.get("voice")
        if p.health and voice is not None and voice.client_id == ctx.client_id:
            changed = core.voice_report.update(
                {name: (entry.status, entry.reason) for name, entry in p.health.items()}
            )
            if changed and core.health is not None:
                core.spawn_task(core.health.run_once())
        return {"ok": True}

    async def worker_failed(self, ctx: RequestContext, p: WorkerFailed) -> dict[str, Any]:
        """Keep a worker's own account of its failure for health, once the process has exited.

        Only for the service the connection was spawned as, and without file system paths:
        `/health` is readable without a token.
        """
        if ctx.client_id != f"worker:{p.service}":
            raise IpcError(
                ERR_PERMISSION, f"client {ctx.client_id!r} is not the {p.service!r} worker"
            )
        reason = without_paths(p.reason)[:MAX_WORKER_REASON_CHARS]
        self._core.workers.report_failure(p.service, reason)
        log.error("worker.reported_failure", service=p.service, reason=reason)
        return {"ok": True}

    # ---- plugins ---------------------------------------------------------------------------------

    async def plugin_status(self, _ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        """What each plugin is doing, and why.

        A plugin the active profile refuses never starts, and it says so during boot - before any
        UI is connected to hear it. Without this request the dashboard can only show an empty
        panel; with it, it can say which plugin is not active in this profile and why.
        """
        plugins = self._core.plugins
        entries = [] if plugins is None else plugins.status()
        return {
            "plugins": [
                {"id": entry.plugin_id, "state": entry.state.value, "reason": entry.reason}
                for entry in entries
            ]
        }

    # ---- stream ----------------------------------------------------------------------------------

    async def stream_session_status(self, _ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        assert self._core.stream_sessions is not None
        return self._core.stream_sessions.status()

    async def stream_funken_top(self, _ctx: RequestContext, p: FunkenTopRequest) -> dict[str, Any]:
        assert self._core.funken_booking is not None
        return {"viewers": self._core.funken_booking.top(p.limit)}
