"""`install(core)`: the whole Settings area, wired onto a running core.

The core installs this like any other extension (see `nox.core.extension`), so this module only
builds the services and registers their IPC requests.

What gets wired:

* `ConfigEditor` on the live configuration plus the user layer (`user.yaml`), with the live
  appliers below, so `config.get` and `config.set` work - and with the security PIN gate, so a
  change to a `security.*` or `privacy.*` setting carries the PIN when one is configured;
* `SecretsService` on the core's keyring store and PIN manager (`secrets.*`), plus
  `security.pin.status` - the one boolean the Settings page needs in order to know whether a
  credential change will ask for a PIN;
* `TwitchAuthService` on the guarded HTTP client (`twitch.auth.*`), plus a background refresher so
  a long session never dies of an expired chat token;
* `PersonalityFile` in the data directory (`personality.*`), installed as the prompt builder's
  personality source, so the system prompt reads it.

Every request is registered for the two UI roles only. Nothing here is reachable from a plugin, a
worker, the pet renderer or a paired phone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext
from nox.ipc.errors import ERR_PERMISSION, IpcError
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

#: Consecutive refresh failures after which the chat connection is reported as limited. A token
#: that cannot be refreshed will expire, and a silent retry loop would let that happen
#: unannounced.
TOKEN_FAILURES_BEFORE_LIMITED = 3


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
    #: Required when the batch touches a `security.*` or `privacy.*` setting and a PIN is set.
    pin: str | None = None


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
        # The orchestrator holds the language a spoken answer uses, so the running conversation
        # follows the setting without a restart. It is None only in a test that never built one.
        if core.orchestrator is not None:
            core.orchestrator.config.default_language = str(value)

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
class TokenHealth:
    """Consecutive Twitch token-refresh failures, and the last reason."""

    consecutive_failures: int = 0
    last_error: str = ""

    @property
    def limited(self) -> bool:
        return self.consecutive_failures >= TOKEN_FAILURES_BEFORE_LIMITED

    def succeeded(self) -> None:
        self.consecutive_failures = 0
        self.last_error = ""

    def failed(self, exc: BaseException) -> None:
        self.consecutive_failures += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        log.warning(
            "settings.twitch_token_refresh_failed",
            error=self.last_error,
            consecutive=self.consecutive_failures,
        )


@dataclass(slots=True)
class SettingsRuntime:
    """Handles the integrator (or a test) needs; `stop()` is called by `NoxCore.stop()`."""

    editor: ConfigEditor
    secrets: SecretsService
    twitch_auth: TwitchAuthService
    personality: PersonalityFile
    token_health: TokenHealth
    _tasks: list[asyncio.Task[None]]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass  # the cancellation above; nothing to report
            except Exception as exc:  # noqa: BLE001 - shutdown continues, but says what broke
                log.warning(
                    "settings.task_stop_failed",
                    task=task.get_name(),
                    error=f"{type(exc).__name__}: {exc}",
                )
        await self.twitch_auth.stop()
        from nox.ai import prompting  # noqa: PLC0415 - avoids an import cycle at module level

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
        gate=security.gate,
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

    # The core and the chat responder both build their system prompt from the personality block;
    # installing the source here is what turns that into "read personality.md".
    from nox.ai import prompting  # noqa: PLC0415 - avoids an import cycle at module level

    prompting.set_personality_source(personality.read)

    token_health = TokenHealth()
    _register(core, editor, secrets, twitch_auth, personality, token_health)

    tasks = [
        asyncio.create_task(
            _twitch_token_loop(twitch_auth, token_health), name="twitch-token-refresh"
        ),
    ]
    log.info("settings.installed", personality=str(personality.path))
    return SettingsRuntime(editor, secrets, twitch_auth, personality, token_health, tasks)


async def _twitch_token_loop(auth: TwitchAuthService, health: TokenHealth) -> None:
    """Keep the stored chat token fresh, and say so when it cannot be kept fresh.

    The Twitch plugin runs in a worker process whose API can read a credential but never write
    one, so the refresh has to happen here. The plugin re-reads the token on every connect attempt
    and reconnects with backoff, so a token renewed here is picked up without the plugin knowing
    anything about the login flow.

    A failure is logged with its type and counted. After a few in a row the integration reports
    itself as limited: a refresh that keeps failing ends in an expired token, and finding that out
    when the chat goes quiet is worse than being told now.
    """
    try:
        await auth.refresh_state_from_secrets()
    except Exception as exc:  # noqa: BLE001 - an unreachable keyring must not kill the task
        log.warning("settings.twitch_state_unavailable", error=type(exc).__name__)
    while True:
        try:
            await auth.ensure_fresh_token()
            health.succeeded()
        except Exception as exc:  # noqa: BLE001 - the loop outlives any single failure
            health.failed(exc)
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL_S)


def _register(
    core: _Core,
    editor: ConfigEditor,
    secrets: SecretsService,
    twitch_auth: TwitchAuthService,
    personality: PersonalityFile,
    health: TokenHealth,
) -> None:
    reg = core.registry.register

    async def h_config_get(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return editor.snapshot()

    async def h_config_set(ctx: RequestContext, p: ConfigSetRequest) -> dict[str, Any]:
        try:
            return await editor.apply(p.values, by=ctx.role, pin=p.pin)
        except PermissionError as exc:
            raise IpcError(ERR_PERMISSION, str(exc)) from exc

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
        return dict(await secrets.set(p.name, p.value, pin=p.pin, by=ctx.role))

    async def h_secrets_delete(ctx: RequestContext, p: SecretDeleteRequest) -> dict[str, Any]:
        return dict(await secrets.delete(p.name, pin=p.pin, by=ctx.role))

    async def h_twitch_start(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return await twitch_auth.start()

    async def h_twitch_status(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        """The login state, plus the refresher's own health when it keeps failing."""
        status = dict(twitch_auth.status())
        if health.limited:
            status["refresh_state"] = "limited"
            status["refresh_error"] = health.last_error
        return status

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
