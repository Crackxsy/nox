"""Twitch OAuth device-code flow, driven through a real `EgressGuard` with a fake inner transport.

Nothing here talks to Twitch: the transport answers canned JSON. What it does exercise for real is
the guard - so a profile that does not allow `id.twitch.tv:443`, or privacy mode OFFLINE, fails the
flow the same way it would in production.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nox.core.state import PrivacyMode
from nox.security.egress import EgressGuard
from nox.security.model import Profile
from nox.security.secrets import InMemorySecretStore
from nox.settings.twitch_auth import (
    SECRET_ACCESS_TOKEN,
    SECRET_CLIENT_ID,
    SECRET_LOGIN,
    SECRET_REFRESH_TOKEN,
    TwitchAuthService,
    TwitchAuthState,
)
from tests.unit.fakes import FakeBus
from tests.unit.settings.conftest import Clock, FakeAudit

ACCESS = "acc3ss-token"  # noqa: S105 - test fixture value
REFRESH = "r3fresh-token"  # noqa: S105 - test fixture value
NEW_ACCESS = "new-acc3ss"  # noqa: S105 - test fixture value


class FakeTransport(httpx.AsyncBaseTransport):
    """Answers each endpoint from a scripted queue and records what was actually sent."""

    def __init__(self, script: dict[str, list[tuple[int, dict[str, Any]]]]) -> None:
        self.script = {key: list(value) for key, value in script.items()}
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path
        self.calls.append((key, request.content.decode() if request.content else ""))
        queue = self.script.get(key)
        if not queue:
            return httpx.Response(404, json={"message": f"no script for {key}"})
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, content=json.dumps(body), headers={"x-test": "1"})


class Privacy:
    def __init__(self, mode: PrivacyMode = PrivacyMode.BALANCED) -> None:
        self.mode = mode


def make_service(
    transport: FakeTransport,
    secrets: InMemorySecretStore,
    audit: FakeAudit,
    bus: FakeBus,
    clock: Clock,
    *,
    privacy: PrivacyMode = PrivacyMode.BALANCED,
    allowlist: tuple[str, ...] = ("id.twitch.tv:443",),
    sleep: Callable[[float], Any] | None = None,
) -> TwitchAuthService:
    profile = Profile(id="companion", rules=[], egress_allowlist=list(allowlist))
    guard = EgressGuard(
        profile=lambda: profile,
        privacy=Privacy(privacy),
        audit=audit,
        transport_factory=lambda: transport,
    )

    async def _no_sleep(_seconds: float) -> None:
        return None

    return TwitchAuthService(
        secrets=secrets,
        client_factory=guard.client,
        bus=bus,
        audit=audit,
        clock=clock,
        sleep=sleep or _no_sleep,
    )


DEVICE_OK = (
    200,
    {
        "device_code": "dev-code",
        "user_code": "ABCD-1234",
        "verification_uri": "https://www.twitch.tv/activate",
        "expires_in": 1800,
        "interval": 5,
    },
)
VALIDATE_OK = (200, {"login": "noxbot", "scopes": ["chat:read", "chat:edit"], "expires_in": 14400})


async def test_start_returns_the_user_code_and_reports_pending(
    secrets: InMemorySecretStore, audit: FakeAudit, bus: FakeBus, clock: Clock
) -> None:
    secrets.set(SECRET_CLIENT_ID, "client-abc")
    transport = FakeTransport(
        {
            "/oauth2/device": [DEVICE_OK],
            "/oauth2/token": [(400, {"message": "authorization_pending"})],
        }
    )
    service = make_service(transport, secrets, audit, bus, clock)

    result = await service.start()

    assert result["user_code"] == "ABCD-1234"
    assert result["verification_uri"] == "https://www.twitch.tv/activate"
    assert service.state is TwitchAuthState.PENDING
    await service.stop()


async def test_a_missing_client_id_is_a_typed_error(
    secrets: InMemorySecretStore, audit: FakeAudit, bus: FakeBus, clock: Clock
) -> None:
    from nox.ipc.errors import ERR_VALIDATION, IpcError

    service = make_service(FakeTransport({}), secrets, audit, bus, clock)

    with pytest.raises(IpcError) as exc:
        await service.start()
    assert exc.value.code == ERR_VALIDATION
    assert exc.value.details["code"] == "twitch.client_id_missing"


async def test_polling_through_pending_to_authorized_stores_the_tokens(
    secrets: InMemorySecretStore, audit: FakeAudit, bus: FakeBus, clock: Clock
) -> None:
    secrets.set(SECRET_CLIENT_ID, "client-abc")
    transport = FakeTransport(
        {
            "/oauth2/device": [DEVICE_OK],
            "/oauth2/token": [
                (400, {"message": "authorization_pending"}),
                (200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 14400}),
            ],
            "/oauth2/validate": [VALIDATE_OK],
        }
    )
    service = make_service(transport, secrets, audit, bus, clock)

    await service.start()
    await service._poll_task  # the background poll task started by `start()`

    assert service.state is TwitchAuthState.AUTHORIZED
    assert secrets.get(SECRET_ACCESS_TOKEN) == f"oauth:{ACCESS}"  # IRC wants the prefix
    assert secrets.get(SECRET_REFRESH_TOKEN) == REFRESH
    assert secrets.get(SECRET_LOGIN) == "noxbot"
    status = service.status()
    assert status["state"] == "authorized"
    assert status["login"] == "noxbot"
    assert status["scopes"] == ["chat:read", "chat:edit"]
    assert ACCESS not in repr(status) and REFRESH not in repr(status)
    assert ACCESS not in audit.blob() and REFRESH not in audit.blob()
    changed = [e for e in bus.published if e.name == "twitch.auth.changed"]
    assert [e.payload["state"] for e in changed] == ["pending", "authorized"]


async def test_an_expired_device_code_ends_the_flow(
    secrets: InMemorySecretStore, audit: FakeAudit, bus: FakeBus, clock: Clock
) -> None:
    secrets.set(SECRET_CLIENT_ID, "client-abc")
    transport = FakeTransport(
        {
            "/oauth2/device": [DEVICE_OK],
            "/oauth2/token": [(400, {"message": "expired_token"})],
        }
    )
    service = make_service(transport, secrets, audit, bus, clock)

    await service.start()
    await service._poll_task

    assert service.state is TwitchAuthState.EXPIRED
    assert secrets.get(SECRET_ACCESS_TOKEN) is None


async def test_ensure_fresh_token_refreshes_shortly_before_expiry(
    secrets: InMemorySecretStore, audit: FakeAudit, bus: FakeBus, clock: Clock
) -> None:
    secrets.set(SECRET_CLIENT_ID, "client-abc")
    secrets.set(SECRET_ACCESS_TOKEN, f"oauth:{ACCESS}")
    secrets.set(SECRET_REFRESH_TOKEN, REFRESH)
    transport = FakeTransport(
        {
            "/oauth2/validate": [VALIDATE_OK],
            "/oauth2/token": [
                (200, {"access_token": NEW_ACCESS, "refresh_token": "r2", "expires_in": 14400})
            ],
        }
    )
    service = make_service(transport, secrets, audit, bus, clock)

    await service.refresh_state_from_secrets()
    assert service.state is TwitchAuthState.AUTHORIZED

    # Well inside the 4 h lifetime: nothing is refreshed.
    assert await service.ensure_fresh_token() is True
    assert secrets.get(SECRET_ACCESS_TOKEN) == f"oauth:{ACCESS}"

    # Inside the refresh skew: the token is renewed before a reconnect can race the expiry.
    clock.advance(14400 - 60)
    assert await service.ensure_fresh_token() is True
    assert secrets.get(SECRET_ACCESS_TOKEN) == f"oauth:{NEW_ACCESS}"
    assert secrets.get(SECRET_REFRESH_TOKEN) == "r2"


async def test_ensure_fresh_token_reports_expired_without_a_refresh_token(
    secrets: InMemorySecretStore, audit: FakeAudit, bus: FakeBus, clock: Clock
) -> None:
    secrets.set(SECRET_CLIENT_ID, "client-abc")
    secrets.set(SECRET_ACCESS_TOKEN, f"oauth:{ACCESS}")
    transport = FakeTransport({"/oauth2/validate": [(401, {"message": "invalid access token"})]})
    service = make_service(transport, secrets, audit, bus, clock)

    await service.refresh_state_from_secrets()

    assert service.state is TwitchAuthState.EXPIRED


async def test_disconnect_removes_every_twitch_token(
    secrets: InMemorySecretStore, audit: FakeAudit, bus: FakeBus, clock: Clock
) -> None:
    for name in (SECRET_ACCESS_TOKEN, SECRET_REFRESH_TOKEN, SECRET_LOGIN, SECRET_CLIENT_ID):
        secrets.set(name, "value")
    service = make_service(FakeTransport({}), secrets, audit, bus, clock)

    assert await service.disconnect() == {"ok": True}

    assert secrets.get(SECRET_ACCESS_TOKEN) is None
    assert secrets.get(SECRET_REFRESH_TOKEN) is None
    assert secrets.get(SECRET_LOGIN) is None
    assert secrets.get(SECRET_CLIENT_ID) == "value"  # the app registration itself is kept
    assert service.state is TwitchAuthState.IDLE


async def test_the_egress_guard_blocks_the_flow_in_offline_mode(
    secrets: InMemorySecretStore, audit: FakeAudit, bus: FakeBus, clock: Clock
) -> None:
    from nox.ipc.errors import ERR_UNAVAILABLE, IpcError

    secrets.set(SECRET_CLIENT_ID, "client-abc")
    transport = FakeTransport({"/oauth2/device": [DEVICE_OK]})
    service = make_service(transport, secrets, audit, bus, clock, privacy=PrivacyMode.OFFLINE)

    with pytest.raises(IpcError) as exc:
        await service.start()
    assert exc.value.code == ERR_UNAVAILABLE
    assert transport.calls == []  # denied before a connection was attempted


async def test_a_profile_without_the_host_blocks_the_flow(
    secrets: InMemorySecretStore, audit: FakeAudit, bus: FakeBus, clock: Clock
) -> None:
    from nox.ipc.errors import IpcError

    secrets.set(SECRET_CLIENT_ID, "client-abc")
    transport = FakeTransport({"/oauth2/device": [DEVICE_OK]})
    service = make_service(
        transport, secrets, audit, bus, clock, allowlist=("api.telegram.org:443",)
    )

    with pytest.raises(IpcError):
        await service.start()
    assert transport.calls == []
