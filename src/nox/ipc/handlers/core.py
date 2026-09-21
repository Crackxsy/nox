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

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.core.state import Mode, PrivacyMode, SystemLevel
from nox.ipc.dispatch import EmptyPayload, RequestContext
from nox.ipc.errors import ERR_PERMISSION, ERR_UNAVAILABLE, IpcError
from nox.security.constants import RESUME_ROLES, USER_KILL_ORIGINS
from nox.security.gate import PinRequiredError, relaxes_privacy
from nox.security.model import Decision

if TYPE_CHECKING:
    from nox.app import NoxCore

log = get_logger(__name__)

__all__ = [
    "PROFILE_FOR_MODE",
    "PUBLIC_STATE_ROOTS",
    "ChatSend",
    "CoreHandlers",
    "FunkenTopRequest",
    "ModeSet",
    "PermissionReply",
    "PetInteract",
    "PrivacySet",
    "SecurityKill",
    "SecurityPanic",
    "SecurityResume",
    "StateGet",
    "VoiceMute",
    "VoicePtt",
    "WorkerHeartbeat",
    "WorkerReady",
    "WorkerRegister",
    "register_core_handlers",
]

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
        reg("security.kill", SecurityKill, self.kill, roles=(*ui, "supervisor"))
        reg("security.panic", SecurityPanic, self.panic, roles=(*ui, "supervisor"))
        reg("security.resume", SecurityResume, self.resume, roles=(*ui, "supervisor"))
        reg("security.permission.reply", PermissionReply, self.permission_reply, roles=("shell",))
        reg("voice.ptt", VoicePtt, self.voice_ptt, roles=("shell",))
        reg("voice.mute", VoiceMute, self.voice_mute, roles=ui)
        reg("chat.send", ChatSend, self.chat_send, roles=ui)
        reg("ai.providers", EmptyPayload, self.ai_providers, roles=ui)
        reg("pet.interact", PetInteract, self.pet_interact, roles=("pet", "shell"))
        # Worker registration is for spawned workers only; see the module docstring.
        reg("worker.register", WorkerRegister, self.worker_register, roles=("worker",))
        reg("worker.ready", WorkerReady, self.worker_ready, roles=("worker",))
        reg("worker.heartbeat", WorkerHeartbeat, self.worker_heartbeat, roles=("worker", "plugin"))
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
        if p.mode is not None:
            await privacy.set_mode(p.mode, by=ctx.role, confirmed=p.confirmed)
        if any(value is not None for value in (p.microphone, p.screen, p.camera)):
            await privacy.set_capture(
                microphone=p.microphone, screen=p.screen, camera=p.camera, by=ctx.role
            )
        await core.state.update("privacy.mode", privacy.mode.value, reason="privacy.set")
        result: dict[str, Any] = privacy.state.model_dump(mode="json")
        return result

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
            raise IpcError(ERR_PERMISSION, exc.reason) from exc

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
        """Leave safe mode.

        Only the user-controlled surfaces may ask: the shell, the dashboard, and the tray or
        hotkey path that reaches the core through the supervisor. The PIN is required after a
        security-path kill - tamper, a broken audit chain, panic; without a PIN configured the
        explicit request is the authorisation, and it is audited as such.
        """
        core = self._core
        assert core.security is not None and core.state is not None and core.bus is not None
        if ctx.role not in RESUME_ROLES:
            raise IpcError(ERR_PERMISSION, f"role {ctx.role!r} may not resume from safe mode")
        killswitch = core.security.killswitch
        pin = core.security.pin
        if killswitch.security_path and pin.is_set():
            status = await pin.verify_pin_async(p.pin, by=ctx.role) if p.pin else None
            pin_ok = status is not None and status.ok
        else:
            pin_ok = True
        ok = await killswitch.resume(pin_ok=pin_ok, by=ctx.role)
        if ok:
            await core.state.update("system.level", SystemLevel.RUNNING.value, reason="resume")
            await core.bus.publish(Event(name=E.SYSTEM_STARTED, payload={"resumed": True}))
            core.ensure_voice_worker()
        return {"ok": ok}

    async def permission_reply(self, _ctx: RequestContext, p: PermissionReply) -> dict[str, Any]:
        assert self._core.security is not None
        ok = self._core.security.engine.reply(
            p.grant_id, p.decision, remember=p.remember, by="user"
        )
        return {"ok": ok}

    # ---- voice, chat, pet ------------------------------------------------------------------------

    async def voice_ptt(self, _ctx: RequestContext, p: VoicePtt) -> dict[str, Any]:
        core = self._core
        assert core.hub is not None
        client_id = core.workers.client_id("voice")
        if client_id is None:
            return {"ok": False, "reason": "voice worker unavailable"}
        await core.hub.request(client_id, "voice.ptt", {"pressed": p.pressed}, timeout=2.0)
        return {"ok": True}

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
            # The state change stands; the worker picks the flag up when it reconnects.
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
        return {"ok": True, "config": core.config.voice.model_dump(mode="json")}

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

    async def worker_heartbeat(self, _ctx: RequestContext, _p: WorkerHeartbeat) -> dict[str, Any]:
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
