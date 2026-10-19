"""`install(core) -> StreamControlRuntime`: Nox's hands on the stream.

Wires, on a started core:

* the OBS overlay with its persistent credential (`nox.stream.overlay`) - IPC `stream.overlay.*`
  for the dashboard and the tray, tool `stream.overlay.setup` for the model;
* Nox's own OBS audio track (`nox.stream.audio_track`) - IPC and tool `stream.audio_track.setup`;
* the automatic stream session when OBS or the Twitch channel goes live (`nox.stream.autostart`);
* login health and notices for Twitch and OBS, and the warning about a desktop pet on a captured
  monitor (`nox.stream.notices`).

Every change to OBS goes through the permission-checked `obs.*` tools, so each of these asks the
user exactly as a direct request would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from nox.core.health import Check
from nox.core.logging import get_logger
from nox.core.modes import switch_mode
from nox.core.state import Mode
from nox.ipc.dispatch import EmptyPayload, RequestContext
from nox.ipc.overlay_tokens import OverlayTokenStore
from nox.security.model import Risk
from nox.settings.devices import OUTPUT_DEVICES, voice_worker_request
from nox.settings.twitch_auth import (
    SECRET_ACCESS_TOKEN,
    SECRET_CLIENT_ID,
    TwitchAuthService,
    TwitchAuthState,
)
from nox.stream.audio_track import AudioTrackRequest, AudioTrackService
from nox.stream.autostart import AutoStreamSession, TwitchLiveWatcher
from nox.stream.notices import AuthWatch, CaptureWarning
from nox.stream.overlay import OverlayService, OverlaySetupRequest
from nox.tools.registry import ToolSpec

log = get_logger(__name__)

UI_ROLES = ("shell", "dashboard")
#: The agent name the core's own stream actions use towards the permission engine.
AGENT = "nox.stream"


@dataclass(slots=True)
class StreamControlRuntime:
    overlay: OverlayService | None
    audio: AudioTrackService
    auto: AutoStreamSession
    watcher: TwitchLiveWatcher
    auth: AuthWatch
    capture: CaptureWarning
    _stopped: bool = field(default=False)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.auto.stop()
        self.auth.stop()
        self.capture.stop()
        await self.watcher.stop()


def _extension(core: Any, name: str) -> Any:
    extensions = getattr(core, "extensions", None) or {}
    return extensions.get(name)


def install(core: Any) -> StreamControlRuntime:
    config = core.config
    executor = core.tool_executor

    def mode() -> str:
        value = core.state.get("assistant.mode") if core.state is not None else ""
        return str(value or Mode.COMPANION.value)

    def language() -> str:
        return str(config.identity.ui_language or "de")

    async def notify(context_key: str, text: str) -> None:
        proactive = _extension(core, "proactive")
        if proactive is None:
            log.warning("stream.notice_unshown", key=context_key, reason="proactive not installed")
            return
        # Lesser-urgent: always a visible toast; never spoken - codes and setup hints are for the
        # screen, and Nox's voice may be on stream.
        await proactive.service.notify(
            "urgent", "resources", text, context_key=context_key, speak=False
        )

    # -- overlay ---------------------------------------------------------------------------------
    tokens = getattr(core, "tokens", None)
    store = cast(OverlayTokenStore | None, getattr(tokens, "overlay", None))
    overlay = (
        OverlayService(
            store,
            executor,
            http_port=lambda: int(core.http.port) if core.http is not None else 0,
            variant=lambda: str(config.pet.variant),
            mode=mode,
            hub=core.hub,
        )
        if store is not None
        else None
    )

    # -- audio track -----------------------------------------------------------------------------
    async def devices() -> list[str] | None:
        return await OUTPUT_DEVICES.refresh(voice_worker_request(core))

    def editor() -> Any:
        settings = _extension(core, "settings")
        return settings.editor if settings is not None else None

    audio = AudioTrackService(executor, devices=devices, editor=editor, mode=mode)

    # -- automatic stream session ----------------------------------------------------------------
    auto_cfg = config.stream.auto_session

    async def switch(target: Mode, by: str) -> Any:
        return await switch_mode(core.state, core.security.engine, core.bus, target, by=by)

    async def not_switched(current: str) -> None:
        await notify(
            "stream.live_elsewhere",
            "OBS ist live - für Chat-Antworten 'lass uns streamen' sagen."
            if language().startswith("de")
            else "OBS is live - say 'let's stream' and I will answer the chat.",
        )
        log.info("stream.live_while_busy", mode=current)

    auto = AutoStreamSession(
        core.bus,
        current_mode=mode,
        switch_mode=switch,
        notify=not_switched,
        from_modes=list(auto_cfg.from_modes),
        enabled=auto_cfg.enabled,
    )
    auto.start()

    settings_rt = _extension(core, "settings")
    twitch: TwitchAuthService | None = settings_rt.twitch_auth if settings_rt is not None else None

    def credentials() -> tuple[str, str] | None:
        if twitch is None or twitch.state is not TwitchAuthState.AUTHORIZED:
            return None
        try:
            client_id = core.security.secrets.get(SECRET_CLIENT_ID)
            token = core.security.secrets.get(SECRET_ACCESS_TOKEN)
        except Exception:  # noqa: BLE001 - an unavailable keyring means "no login"
            return None
        if not client_id or not token:
            return None
        return str(client_id), str(token).removeprefix("oauth:")

    async def twitch_live() -> None:
        await auto.live(source="twitch")

    watcher = TwitchLiveWatcher(
        channel=lambda: str(config.stream.twitch.channel),
        credentials=credentials,
        client_factory=lambda **kw: cast(httpx.AsyncClient, core.security.egress.client(**kw)),
        current_mode=mode,
        on_live=twitch_live,
        interval_s=float(auto_cfg.twitch_live_poll_s) if auto_cfg.enabled else 0.0,
    )
    if "twitch" in config.plugins.enabled:
        watcher.start()

    # -- notices and health ----------------------------------------------------------------------
    auth = AuthWatch(
        core.bus,
        twitch,
        notify=notify,
        language=language,
        twitch_enabled=lambda: "twitch" in config.plugins.enabled,
    )
    auth.start()

    def pet_open() -> bool:
        hub = getattr(core, "hub", None)
        return hub is not None and any(c.role == "pet" for c in hub.clients())

    capture = CaptureWarning(
        core.bus, executor, pet_open=pet_open, notify=notify, language=language, mode=mode
    )
    capture.start()

    health = getattr(core, "health", None)
    if health is not None:
        if "twitch" in config.plugins.enabled:
            health.add_check(Check("twitch.login", auth.twitch_health))
        if "obs" in config.plugins.enabled:
            health.add_check(Check("obs.login", auth.obs_health))

    _register(core, overlay, audio)
    log.info("stream.control_installed", overlay=overlay is not None)
    return StreamControlRuntime(overlay, audio, auto, watcher, auth, capture)


def _register(core: Any, overlay: OverlayService | None, audio: AudioTrackService) -> None:
    reg = core.registry.register

    async def h_overlay_status(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        if overlay is None:
            return {"configured": False, "available": False}
        return {"available": True, **overlay.status()}

    async def h_overlay_setup(ctx: RequestContext, p: OverlaySetupRequest) -> dict[str, Any]:
        if overlay is None:
            return {"ok": False, "error": "overlay credentials are not available"}
        return await overlay.setup(p, agent=ctx.role, origin="local")

    async def h_overlay_revoke(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        if overlay is None:
            return {"ok": False, "error": "overlay credentials are not available"}
        return await overlay.revoke()

    async def h_audio_setup(ctx: RequestContext, p: AudioTrackRequest) -> dict[str, Any]:
        return await audio.setup(p, agent=ctx.role, origin="local")

    reg("stream.overlay.status", EmptyPayload, h_overlay_status, roles=UI_ROLES)
    reg("stream.overlay.setup", OverlaySetupRequest, h_overlay_setup, roles=UI_ROLES)
    reg("stream.overlay.revoke", EmptyPayload, h_overlay_revoke, roles=UI_ROLES)
    reg("stream.audio_track.setup", AudioTrackRequest, h_audio_setup, roles=UI_ROLES)

    # The model's way to the same actions. Low risk on their own: the one change they make in
    # OBS goes through `obs.overlay.setup` / `obs.audio_track.setup`, which ask the user.
    async def t_overlay(payload: dict[str, Any]) -> dict[str, Any]:
        if overlay is None:
            return {"ok": False, "error": "overlay credentials are not available"}
        return await overlay.setup(
            OverlaySetupRequest.model_validate(payload), agent=AGENT, origin="chat"
        )

    async def t_audio(payload: dict[str, Any]) -> dict[str, Any]:
        return await audio.setup(
            AudioTrackRequest.model_validate(payload), agent=AGENT, origin="chat"
        )

    core.tool_registry.register(
        ToolSpec(
            name="stream.overlay.setup",
            description=(
                "Show Nox on stream: create or update the 'Nox Overlay' browser source in the "
                "current OBS scene, with a URL that survives restarts. The user confirms the OBS "
                "change; the URL's credential is never shown."
            ),
            input_model=OverlaySetupRequest,
            risk=Risk.LOW,
            handler=t_overlay,
            local=True,
            timeout_s=90.0,
        )
    )
    core.tool_registry.register(
        ToolSpec(
            name="stream.audio_track.setup",
            description=(
                "Give Nox its own OBS audio track: Nox speaks into the virtual audio cable (and "
                "still into the headphones), OBS captures it as the 'Nox Voice' source, optionally "
                "on chosen tracks 1-6. Needs VB-Audio Virtual Cable; the user confirms the OBS "
                "change."
            ),
            input_model=AudioTrackRequest,
            risk=Risk.LOW,
            handler=t_audio,
            local=True,
            timeout_s=90.0,
        )
    )
