"""`install(core)` - the whole Mobile Companion wiring in one function (Spec v0.8, EPIC-17).

`src/nox/app.py` is shared between agents and is deliberately *not* touched by this release: the
integrator calls `nox.remote.install.install(core)` once, after `NoxCore.start()`, and everything
below exists; without that call the feature is inert. It is also inert when `remote.enabled` is
false, which is the shipped default - pairing, the remote kill switch and remote privacy switching
never come up on their own.

What this wires:
  * `RemoteRepository` on the core database (migration `0008_remote.sql`),
  * `PairingService` + `RemoteCommandPolicy` + `RemoteService` on the core bus, so every
    `remote.message` from the `telegram` plugin is policed here,
  * outbound text through the *tool* pipeline (`telegram.send`, low risk, rate-limited in the
    plugin) - never a direct HTTP call from the core,
  * chat through the existing `Orchestrator` with `speak=False`: same router, same prompt builder,
    same personality as the desktop, only without a voice,
  * `RemoteNotifier` on the configured event allow-list,
  * three IPC requests for the dashboard's "Remote" page, `shell`/`dashboard` only: `remote.pair.
    start`, `remote.devices.list`, `remote.unpair`. The `remote` role itself is never granted any
    of them - a phone cannot pair a second phone or revoke another device.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_NOT_FOUND, ERR_UNAVAILABLE, IpcError
from nox.remote.models import DeviceRow
from nox.remote.notify import RemoteNotifier
from nox.remote.pairing import PairingService
from nox.remote.policy import RemoteCommandPolicy, RemoteRateLimiter
from nox.remote.repo import RemoteRepository
from nox.remote.service import RemoteService

log = get_logger(__name__)

#: The plugin tool the core uses to answer on the phone's channel. Declared by
#: `plugins/telegram/manifest.yaml`, registered in the shared `ToolRegistry` when the plugin starts.
SEND_TOOL = "telegram.send"

#: UI roles allowed to pair and revoke. `remote` is absent on purpose (see the module docstring).
LOCAL_ROLES = ("shell", "dashboard")


class PairStart(BaseModel):
    name: str = Field(default="", max_length=64)


class Unpair(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)


class EmptyRequest(BaseModel):
    pass


def _device_json(device: DeviceRow) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "channel": device.channel,
        "paired_at": device.paired_at.isoformat(),
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "revoked_at": device.revoked_at.isoformat() if device.revoked_at else None,
        "revoked_reason": device.revoked_reason,
    }


def install(core: Any) -> None:
    """Wire the remote area into a started `NoxCore`. Safe to call once; a second call raises from
    `RequestRegistry.register` rather than silently shadowing the first."""
    cfg = core.config.remote
    repo = RemoteRepository(core.db)
    pairing = PairingService(
        repo,
        bus=core.bus,
        audit=core.security.audit,
        ttl_s=cfg.pair_code_ttl_s,
        max_devices=cfg.max_devices,
    )

    async def send(chat_id: str, text: str) -> None:
        """Outbound goes through the tool pipeline so it is permission-checked, rate-limited and
        audited like any other side effect - the core never talks to the Bot API itself."""
        result = await core.tool_executor.call(
            "remote",
            SEND_TOOL,
            {"text": text, "chat_id": chat_id},
            mode=str(core.state.get("assistant.mode")),
            origin="remote",
        )
        if not getattr(result, "ok", False):
            log.warning("remote.send_denied", reason=getattr(result, "error", ""))

    async def chat(text: str) -> str:
        """Phone chat: the normal orchestrator, `speak=False`. No separate prompt, no relaxed
        profile, and the active privacy mode applies exactly as it does on the desktop."""
        turn = await core.orchestrator.handle_text(text, speak=False)
        return str(getattr(turn, "response", ""))

    def status() -> dict[str, str]:
        """Allow-listed scalars only (Spec §3.3): never a transcript, memory entry or
        chat history."""
        privacy = core.security.privacy
        return {
            "Modus": str(core.state.get("assistant.mode")),
            "Privatsphäre": privacy.mode.value,
            "Not-Aus": "aktiv" if core.security.killswitch.is_engaged() else "aus",
            "Pet": str(core.state.get("assistant.pet_functional")),
            "System": str(core.state.get("system.level")),
        }

    service = RemoteService(
        repo=repo,
        pairing=pairing,
        policy=RemoteCommandPolicy(
            rate_limiter=RemoteRateLimiter(
                per_minute=cfg.rate_limit_per_minute, burst=cfg.rate_limit_burst
            ),
            chat_enabled=cfg.chat_enabled,
        ),
        killswitch=core.security.killswitch,
        privacy=core.security.privacy,
        send=send,
        status=status,
        chat=chat if cfg.chat_enabled else None,
        bus=core.bus,
        audit=core.security.audit,
    )

    notifier = RemoteNotifier(
        bus=core.bus,
        send=lambda text: send("", text),
        events=cfg.notifications.events,
        quiet_hours=core.config.attention.quiet_hours,
        active_zone=lambda: core.security.privacy.active_zone,
        has_device=lambda: any(not d.revoked for d in repo.list_devices()),
        enabled=cfg.notifications.enabled,
    )

    # -- IPC surface for the dashboard's "Remote" page ------------------------------------------

    def _require_enabled() -> None:
        if not cfg.enabled:
            raise IpcError(ERR_UNAVAILABLE, "remote companion is disabled (remote.enabled)")

    async def h_pair_start(_ctx: RequestContext, p: PairStart) -> dict[str, Any]:
        _require_enabled()
        start = await pairing.start(device_name=p.name)
        return {
            "pairing_id": start.pairing_id,
            "code": start.code,
            "expires_at": start.expires_at.isoformat(),
            "ttl_s": start.ttl_s,
        }

    async def h_devices(_ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        return {"devices": [_device_json(d) for d in pairing.devices()], "enabled": cfg.enabled}

    async def h_unpair(_ctx: RequestContext, p: Unpair) -> dict[str, Any]:
        _require_enabled()
        if repo.get_device(p.device_id) is None:
            raise IpcError(ERR_NOT_FOUND, "no such device")
        ok = await pairing.revoke(p.device_id, reason="dashboard")
        return {"ok": ok, "device_id": p.device_id}

    reg = core.registry.register
    reg("remote.pair.start", PairStart, h_pair_start, roles=LOCAL_ROLES)
    reg("remote.devices.list", EmptyRequest, h_devices, roles=LOCAL_ROLES)
    reg("remote.unpair", Unpair, h_unpair, roles=LOCAL_ROLES)

    core.remote_repo = repo
    core.remote_pairing = pairing
    core.remote_service = service
    core.remote_notifier = notifier

    if not cfg.enabled:
        log.info("remote.disabled", note="IPC surface registered, no bot, no notifications")
        return
    service.start()
    notifier.start()
    log.info("remote.installed", events=len(cfg.notifications.events))
