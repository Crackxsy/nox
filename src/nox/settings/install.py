"""`install(core)`: the whole Settings area in one function (EPIC-21).

`src/nox/app.py` already runs `("settings", True)` in its extension loop, so this module only has
to build the services and register their IPC requests - the same shape as `nox.remote.install` and
`nox.clips.install`.

What gets wired:
  * `ConfigEditor` on the live `NoxConfig` plus the User layer (`user.yaml`), with the live
    appliers below, so `config.get`/`config.set` work;
  * `SecretsService` on the core's keyring store and PIN manager (`secrets.*`), plus
    `security.pin.status` - the one boolean the Settings page needs to know whether a secret
    change has to carry a PIN (#23);
  * `TwitchAuthService` on `core.security.egress.client` (`twitch.auth.*`), plus a background
    refresher so a long session never dies of an expired chat token;
  * `PersonalityFile` in the data directory (`personality.*`), installed as the prompt builder's
    personality source so `nox.app`'s existing `build_system_prompt(...)` call reads it.

Every request is registered for `("shell", "dashboard")` only. Nothing here is reachable from a
plugin, a worker, the pet renderer or a paired phone.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext
from nox.settings.editor import Applier, ConfigEditor
from nox.settings.layers import resolve_defaults_path, resolve_user_config_path
from nox.settings.personality import PERSONALITY_FILENAME, PersonalityFile
from nox.settings.schema import LIVE_APPLY_PATHS
from nox.settings.secrets_ipc import SecretsService
from nox.settings.twitch_auth import TwitchAuthService

log = get_logger(__name__)

#: UI roles allowed to read or change settings. Deliberately no `plugin`, `worker`, `pet`,
#: `remote` or `supervisor`: a phone must not be able to rewrite the configuration or the keyring.
UI_ROLES = ("shell", "dashboard")

#: How often the background task checks whether the Twitch token is close to expiring.
TOKEN_REFRESH_INTERVAL_S = 300.0


class _Core(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test can pass a stand-in)."""

    config: Any
    bus: Any
    security: Any
    registry: Any
    orchestrator: Any


# ---- IPC payload models -------------------------------------------------------------------------


class ConfigSetRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class SecretSetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=4096)
    #: Required whenever a PIN is configured; verified exactly like `security.resume` does.
    pin: str | None = None


class SecretDeleteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    pin: str | None = None


class PersonalitySetRequest(BaseModel):
    text: str = Field(max_length=32_000)


# ---- live appliers ------------------------------------------------------------------------------


def build_appliers(core: _Core) -> dict[str, Applier]:
    """The settings the running core can genuinely adopt without a restart.

    Anything not in here is written to `user.yaml` and reported as `restart_required` - the
    honest answer, rather than a UI that claims a change took effect when it did not.
    """
    config = core.config

    async def set_identity_name(value: Any) -> None:
        config.identity.name = str(value)

    async def set_user_display_name(value: Any) -> None:
        # Read per turn by `NoxCore._system_prompt`, so the next answer already uses it.
        config.identity.user_display_name = str(value)

    async def set_ui_language(value: Any) -> None:
        config.identity.ui_language = str(value)
        orchestrator = getattr(core, "orchestrator", None)
        if orchestrator is not None:
            orchestrator.config.default_language = str(value)

    async def set_pet_variant(value: Any) -> None:
        # The renderer takes the variant as a query flag, so the shell re-points the pet page when
        # it sees `settings.changed`; the core-side value is what it re-reads.
        config.pet.variant = str(value)

    def capture_applier(kind: str) -> Applier:
        async def apply(value: Any) -> None:
            config.privacy.capture.__setattr__(kind, bool(value))
            await core.security.privacy.set_capture(**{kind: bool(value)}, by="settings")

        return apply

    return {
        "identity.name": set_identity_name,
        "identity.user_display_name": set_user_display_name,
        "identity.ui_language": set_ui_language,
        "pet.variant": set_pet_variant,
        "privacy.capture.microphone": capture_applier("microphone"),
        "privacy.capture.camera": capture_applier("camera"),
        "privacy.capture.screen": capture_applier("screen"),
    }


# ---- runtime ------------------------------------------------------------------------------------


@dataclass(slots=True)
class SettingsRuntime:
    """Handles the integrator (or a test) needs; `stop()` is called by `NoxCore.stop()`."""

    editor: ConfigEditor
    secrets: SecretsService
    twitch_auth: TwitchAuthService
    personality: PersonalityFile
    _tasks: list[asyncio.Task[None]]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.twitch_auth.stop()
        from nox.ai import prompting

        prompting.set_personality_source(None)


def install(core: _Core) -> SettingsRuntime:
    cfg = core.config
    security = core.security

    appliers: Mapping[str, Applier] = build_appliers(core)
    missing = LIVE_APPLY_PATHS - set(appliers)
    if missing:  # pragma: no cover - a coding error, caught by a unit test before it ships
        log.error("settings.live_applier_missing", paths=sorted(missing))

    editor = ConfigEditor(
        config=cfg,
        defaults_path=resolve_defaults_path(),
        user_config_path=resolve_user_config_path(),
        appliers=appliers,
        audit=security.audit,
        bus=core.bus,
    )
    secrets = SecretsService(security.secrets, pin=security.pin, audit=security.audit)
    twitch_auth = TwitchAuthService(
        secrets=security.secrets,
        client_factory=security.egress.client,
        bus=core.bus,
        audit=security.audit,
    )
    personality = PersonalityFile(
        cfg.paths.data_dir / PERSONALITY_FILENAME,
        assistant_name=cfg.identity.name,
        user_name=cfg.identity.user_display_name,
    )
    personality.ensure()

    # `nox.app` and `nox.stream.responder` both call `build_system_prompt(DECIDED_PERSONALITY_
    # BLOCK, ...)`; installing the source here is what turns that into "read personality.md".
    from nox.ai import prompting

    prompting.set_personality_source(personality.read)

    _register(core, editor, secrets, twitch_auth, personality)

    tasks = [
        asyncio.create_task(_twitch_token_loop(twitch_auth), name="twitch-token-refresh"),
    ]
    log.info("settings.installed", personality=str(personality.path))
    return SettingsRuntime(editor, secrets, twitch_auth, personality, tasks)


async def _twitch_token_loop(auth: TwitchAuthService) -> None:
    """Keep the stored chat token fresh.

    The Twitch plugin runs in a worker process whose Plugin API can *read* a secret but never write
    one, so the refresh has to happen here. It re-reads `nox/twitch/oauth_token` on every connect
    attempt, and its IRC client reconnects with backoff, so a token renewed here is picked up
    without the plugin knowing anything about OAuth.
    """
    try:
        await auth.refresh_state_from_secrets()
    except Exception as exc:  # noqa: BLE001 - an unreachable keyring must not kill the task
        log.warning("settings.twitch_state_unavailable", error=type(exc).__name__)
    while True:
        with contextlib.suppress(Exception):
            await auth.ensure_fresh_token()
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL_S)


def _register(
    core: _Core,
    editor: ConfigEditor,
    secrets: SecretsService,
    twitch_auth: TwitchAuthService,
    personality: PersonalityFile,
) -> None:
    reg = core.registry.register

    async def h_config_get(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return editor.snapshot()

    async def h_config_set(ctx: RequestContext, p: ConfigSetRequest) -> dict[str, Any]:
        return await editor.apply(p.values, by=ctx.role)

    async def h_pin_status(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        """Whether a PIN is configured - never the PIN, its hash, its length or its algorithm.

        The Settings page needs exactly this one boolean to know that `secrets.set`/`secrets.delete`
        will ask for a PIN (#23). The alternatives are worse: prompting for a PIN nobody set, or
        sending a request the core is guaranteed to refuse and calling that an error.
        """
        return {"configured": bool(core.security.pin.is_set())}

    async def h_secrets_status(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return dict(secrets.status())

    async def h_secrets_set(ctx: RequestContext, p: SecretSetRequest) -> dict[str, Any]:
        return dict(secrets.set(p.name, p.value, pin=p.pin, by=ctx.role))

    async def h_secrets_delete(ctx: RequestContext, p: SecretDeleteRequest) -> dict[str, Any]:
        return dict(secrets.delete(p.name, pin=p.pin, by=ctx.role))

    async def h_twitch_start(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return await twitch_auth.start()

    async def h_twitch_status(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return twitch_auth.status()

    async def h_twitch_disconnect(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return await twitch_auth.disconnect()

    async def h_personality_get(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return {"text": personality.read(), "path": str(personality.path)}

    async def h_personality_set(ctx: RequestContext, p: PersonalitySetRequest) -> dict[str, Any]:
        personality.write(p.text)
        core.security.audit.append(
            actor=ctx.role,
            tool="settings",
            action="personality.set",
            target=str(personality.path),
            decision="allow",
            result="ok",
            details={"chars": str(len(p.text))},
        )
        return {"ok": True}

    reg("config.get", EmptyPayload, h_config_get, roles=UI_ROLES)
    reg("config.set", ConfigSetRequest, h_config_set, roles=UI_ROLES)
    reg("security.pin.status", EmptyPayload, h_pin_status, roles=UI_ROLES)
    reg("secrets.status", EmptyPayload, h_secrets_status, roles=UI_ROLES)
    reg("secrets.set", SecretSetRequest, h_secrets_set, roles=UI_ROLES)
    reg("secrets.delete", SecretDeleteRequest, h_secrets_delete, roles=UI_ROLES)
    reg("twitch.auth.start", EmptyPayload, h_twitch_start, roles=UI_ROLES)
    reg("twitch.auth.status", EmptyPayload, h_twitch_status, roles=UI_ROLES)
    reg("twitch.auth.disconnect", EmptyPayload, h_twitch_disconnect, roles=UI_ROLES)
    reg("personality.get", EmptyPayload, h_personality_get, roles=UI_ROLES)
    reg("personality.set", PersonalitySetRequest, h_personality_set, roles=UI_ROLES)
