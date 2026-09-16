"""Twitch login without a copy-pasted token: OAuth 2.0 Device Code Grant.

Until now the only way to connect Twitch was to generate an `oauth:...` token by hand and paste it
into the onboarding wizard. The Device Code Grant is the flow Twitch documents for exactly this
situation - a public client with no redirect URL and no client secret:

1. `POST https://id.twitch.tv/oauth2/device` with `client_id` and `scopes` returns a short
   `user_code` and a `verification_uri` the user opens in a browser,
2. `POST https://id.twitch.tv/oauth2/token` with `grant_type=urn:ietf:params:oauth:grant-type:
   device_code` is polled at the interval Twitch dictates until the user approves (or the code
   expires),
3. `GET https://id.twitch.tv/oauth2/validate` tells us which account approved it (`login`), which
   scopes it really has, and how long the token is still valid,
4. `POST .../token` with `grant_type=refresh_token` renews it later.

Every call goes through the injected client factory, which in production is
`core.security.egress.client()` - so `id.twitch.tv:443` has to be on the active profile's egress
allow-list, the attempt is audited by the guard, and the whole flow is simply impossible in privacy
mode PRIVATE or OFFLINE. Tokens go straight into the keyring; no token, refresh token or client id
is ever logged, audited or put into an event payload.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import httpx

from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.ipc.errors import ERR_UNAVAILABLE, ERR_VALIDATION, IpcError
from nox.security.model import AuditLog, SecretStore

log = get_logger(__name__)

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # noqa: S105 - an endpoint URL, not a credential
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: Exactly what the chat bot needs: read chat and send chat. No moderation, no channel management.
SCOPES = "chat:read chat:edit"

SECRET_CLIENT_ID = "nox/twitch/client_id"  # noqa: S105 - a secret *name*, not a value
SECRET_ACCESS_TOKEN = "nox/twitch/oauth_token"  # noqa: S105 - a secret *name*, not a value
SECRET_REFRESH_TOKEN = "nox/twitch/refresh_token"  # noqa: S105 - a secret *name*, not a value
SECRET_LOGIN = "nox/twitch/bot_username"  # noqa: S105 - a secret *name*, not a value

#: Refresh this far before the token actually expires, so a reconnect never races the expiry.
REFRESH_SKEW_S = 600.0
#: Twitch's documented floor; a server-sent `interval` below this is clamped up to it.
MIN_POLL_INTERVAL_S = 5.0
HTTP_TIMEOUT_S = 15.0


class TwitchAuthState(StrEnum):
    IDLE = "idle"  # no token stored
    PENDING = "pending"  # waiting for the user to approve the device code
    AUTHORIZED = "authorized"
    EXPIRED = "expired"  # the device code ran out, or the token can no longer be refreshed
    ERROR = "error"


ClientFactory = Callable[..., httpx.AsyncClient]
Sleeper = Callable[[float], Awaitable[None]]


class TwitchAuthService:
    """Owns the device-code flow and the stored Twitch tokens of one core."""

    def __init__(
        self,
        *,
        secrets: SecretStore,
        client_factory: ClientFactory,
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Sleeper | None = None,
        refresh_skew_s: float = REFRESH_SKEW_S,
    ) -> None:
        self._secrets = secrets
        self._client_factory = client_factory
        self._bus = bus
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._skew = refresh_skew_s

        self._state = TwitchAuthState.IDLE
        self._login = ""
        self._scopes: list[str] = []
        self._expires_at: datetime | None = None
        self._error = ""
        self._poll_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # -- status ----------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """The `twitch.auth.status` response; `login`/`expires_at`/`scopes` only when known."""
        payload: dict[str, Any] = {"state": self._state.value}
        if self._login:
            payload["login"] = self._login
        if self._expires_at is not None:
            payload["expires_at"] = self._expires_at.isoformat()
        if self._scopes:
            payload["scopes"] = list(self._scopes)
        if self._error:
            payload["error"] = self._error
        return payload

    @property
    def state(self) -> TwitchAuthState:
        return self._state

    # -- device code flow ------------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Ask Twitch for a device code and begin polling in the background."""
        client_id = self._secret(SECRET_CLIENT_ID)
        if not client_id:
            raise IpcError(
                ERR_VALIDATION,
                "no Twitch client id stored; register an application at "
                "https://dev.twitch.tv/console/apps and save its client id as "
                f"{SECRET_CLIENT_ID}",
                details={"code": "twitch.client_id_missing"},
            )
        data = await self._post(
            DEVICE_URL, {"client_id": client_id, "scopes": SCOPES}, what="device code"
        )
        device_code = str(data.get("device_code") or "")
        user_code = str(data.get("user_code") or "")
        verification_uri = str(
            data.get("verification_uri") or data.get("verification_uri_complete") or ""
        )
        if not device_code or not user_code:
            raise IpcError(ERR_UNAVAILABLE, "Twitch returned no device code")
        expires_in = float(data.get("expires_in") or 1800)
        interval = max(float(data.get("interval") or MIN_POLL_INTERVAL_S), MIN_POLL_INTERVAL_S)

        await self._cancel_poll()
        self._set_state(TwitchAuthState.PENDING, error="")
        self._audit_action("twitch.auth.start")
        self._poll_task = asyncio.create_task(
            self._poll(client_id, device_code, interval, expires_in), name="twitch-device-poll"
        )
        await self._publish()
        return {
            "user_code": user_code,
            "verification_uri": verification_uri,
            "expires_in": expires_in,
            "interval": interval,
        }

    async def _poll(
        self, client_id: str, device_code: str, interval: float, expires_in: float
    ) -> None:
        """Poll until the user approves, the code expires, or Twitch reports a hard failure."""
        deadline = self._clock() + timedelta(seconds=expires_in)
        try:
            while self._clock() < deadline:
                await self._sleep(interval)
                try:
                    data = await self._post(
                        TOKEN_URL,
                        {
                            "client_id": client_id,
                            "device_code": device_code,
                            "grant_type": DEVICE_GRANT,
                        },
                        what="device token",
                        tolerate_400=True,
                    )
                except IpcError as exc:
                    self._set_state(TwitchAuthState.ERROR, error=exc.message)
                    await self._publish()
                    return
                error = str(data.get("message") or data.get("error") or "").lower()
                if data.get("access_token"):
                    await self._store_tokens(client_id, data)
                    return
                if "authorization_pending" in error or "authorization pending" in error:
                    continue
                if "slow_down" in error or "slow down" in error:
                    interval += MIN_POLL_INTERVAL_S
                    continue
                if "expired" in error:
                    break
                self._set_state(TwitchAuthState.ERROR, error=error or "unexpected token response")
                await self._publish()
                return
            self._set_state(TwitchAuthState.EXPIRED, error="device code expired")
            await self._publish()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise

    # -- tokens ----------------------------------------------------------------------------------

    async def _store_tokens(self, client_id: str, data: dict[str, Any]) -> None:
        """Persist a token pair and learn who it belongs to. Values never leave the keyring."""
        access = str(data.get("access_token") or "")
        refresh = str(data.get("refresh_token") or "")
        if not access:  # pragma: no cover - guarded by the caller
            return
        # The IRC client expects the `oauth:` prefix; store it once here rather than in every
        # consumer.
        prefixed = access if access.startswith("oauth:") else f"oauth:{access}"
        self._secrets.set(SECRET_ACCESS_TOKEN, prefixed)
        if refresh:
            self._secrets.set(SECRET_REFRESH_TOKEN, refresh)
        await self._validate(client_id, access)
        self._audit_action("twitch.auth.authorized")

    async def _validate(self, client_id: str, access_token: str) -> bool:
        """`/oauth2/validate`: learn `login`, `scopes` and the remaining lifetime."""
        try:
            async with self._client_factory(timeout=HTTP_TIMEOUT_S) as client:
                response = await client.get(
                    VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}"}
                )
        except Exception as exc:  # noqa: BLE001 - egress denied, offline, TLS, ...
            self._set_state(TwitchAuthState.ERROR, error=f"validate failed: {type(exc).__name__}")
            await self._publish()
            return False
        if response.status_code != 200:
            self._set_state(TwitchAuthState.EXPIRED, error="token rejected by Twitch")
            await self._publish()
            return False
        body = self._json(response)
        self._login = str(body.get("login") or "")
        self._scopes = [str(s) for s in (body.get("scopes") or [])]
        expires_in = float(body.get("expires_in") or 0.0)
        self._expires_at = self._clock() + timedelta(seconds=expires_in) if expires_in else None
        if self._login:
            self._secrets.set(SECRET_LOGIN, self._login)
        self._set_state(TwitchAuthState.AUTHORIZED, error="")
        log.info("twitch.auth.authorized", login=self._login, scopes=self._scopes)
        await self._publish()
        return True

    async def refresh_state_from_secrets(self) -> None:
        """Boot-time: report honestly whether the stored token still works, without guessing."""
        access = self._stored_access_token()
        if not access:
            self._set_state(TwitchAuthState.IDLE, error="")
            return
        client_id = self._secret(SECRET_CLIENT_ID) or ""
        if await self._validate(client_id, access):
            return
        await self.ensure_fresh_token(force=True)

    async def ensure_fresh_token(self, *, force: bool = False) -> bool:
        """Refresh the access token when it is expired or about to be; called before connecting.

        Returns whether a usable token is available afterwards. Never raises: a Twitch outage or a
        privacy mode that forbids egress must degrade the chat bot, not the core.
        """
        async with self._lock:
            if not force and not self._needs_refresh():
                return self._stored_access_token() is not None
            refresh = self._secret(SECRET_REFRESH_TOKEN)
            client_id = self._secret(SECRET_CLIENT_ID)
            if not refresh or not client_id:
                if self._state is TwitchAuthState.AUTHORIZED:
                    self._set_state(TwitchAuthState.EXPIRED, error="no refresh token stored")
                    await self._publish()
                return self._stored_access_token() is not None
            try:
                data = await self._post(
                    TOKEN_URL,
                    {
                        "client_id": client_id,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh,
                    },
                    what="token refresh",
                )
            except IpcError as exc:
                self._set_state(TwitchAuthState.ERROR, error=exc.message)
                await self._publish()
                return False
            if not data.get("access_token"):
                self._set_state(TwitchAuthState.EXPIRED, error="refresh token rejected")
                await self._publish()
                return False
            await self._store_tokens(client_id, data)
            self._audit_action("twitch.auth.refresh")
            return self._state is TwitchAuthState.AUTHORIZED

    def _needs_refresh(self) -> bool:
        if self._stored_access_token() is None:
            return True
        if self._expires_at is None:
            return False
        return self._clock() >= self._expires_at - timedelta(seconds=self._skew)

    # -- disconnect ------------------------------------------------------------------------------

    async def disconnect(self) -> dict[str, Any]:
        """Forget the Twitch login entirely: both tokens and the bot username leave the keyring."""
        await self._cancel_poll()
        for name in (SECRET_ACCESS_TOKEN, SECRET_REFRESH_TOKEN, SECRET_LOGIN):
            with contextlib.suppress(Exception):
                self._secrets.delete(name)
        self._login = ""
        self._scopes = []
        self._expires_at = None
        self._set_state(TwitchAuthState.IDLE, error="")
        self._audit_action("twitch.auth.disconnect")
        await self._publish()
        return {"ok": True}

    async def stop(self) -> None:
        await self._cancel_poll()

    # -- helpers ---------------------------------------------------------------------------------

    async def _cancel_poll(self) -> None:
        task, self._poll_task = self._poll_task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _post(
        self,
        url: str,
        data: dict[str, str],
        *,
        what: str,
        tolerate_400: bool = False,
    ) -> dict[str, Any]:
        try:
            async with self._client_factory(timeout=HTTP_TIMEOUT_S) as client:
                response = await client.post(url, data=data)
        except Exception as exc:  # noqa: BLE001 - egress denied, offline, TLS, ...
            raise IpcError(
                ERR_UNAVAILABLE, f"{what} request failed: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code == 400 and tolerate_400:
            return self._json(response)
        if response.status_code >= 400:
            # Twitch's error body names the problem (e.g. "invalid client"); it carries no secret.
            raise IpcError(
                ERR_UNAVAILABLE,
                f"{what} rejected by Twitch ({response.status_code}): "
                f"{self._json(response).get('message', '')}".strip(),
            )
        return self._json(response)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _secret(self, name: str) -> str | None:
        try:
            return self._secrets.get(name)
        except Exception as exc:  # noqa: BLE001 - an unavailable keyring is "not configured"
            log.warning("twitch.auth.secret_unavailable", name=name, error=type(exc).__name__)
            return None

    def _stored_access_token(self) -> str | None:
        """The raw access token (the keyring value carries the IRC `oauth:` prefix)."""
        value = self._secret(SECRET_ACCESS_TOKEN)
        if not value:
            return None
        return value[len("oauth:") :] if value.startswith("oauth:") else value

    def _set_state(self, state: TwitchAuthState, *, error: str) -> None:
        self._state = state
        self._error = error

    async def _publish(self) -> None:
        if self._bus is None:
            return
        payload: dict[str, Any] = {"state": self._state.value, "login": self._login}
        await self._bus.publish(Event(name=E.TWITCH_AUTH_CHANGED, payload=payload))

    def _audit_action(self, action: str) -> None:
        if self._audit is None:
            return
        self._audit.append(
            actor="user",
            tool="settings",
            action=action,
            target=SECRET_ACCESS_TOKEN,  # the name; no token value ever reaches the audit log
            decision="allow",
            result="ok",
        )
