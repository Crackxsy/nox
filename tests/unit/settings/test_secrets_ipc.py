"""`secrets.*`: known names only, PIN-gated, rate-limited, and never a value anywhere."""

from __future__ import annotations

import pytest

from nox.ipc.errors import ERR_PERMISSION, ERR_RATE_LIMITED, ERR_VALIDATION, IpcError
from nox.security.secrets import InMemorySecretStore, PinManager
from nox.settings.secrets_ipc import KNOWN_SECRETS, SecretsService
from tests.unit.settings.conftest import FakeAudit

TOKEN = "s3cret-oauth-value"  # noqa: S105 - test fixture value


class Ticker:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_status_reports_presence_only(secrets: InMemorySecretStore, audit: FakeAudit) -> None:
    secrets.set("nox/twitch/oauth_token", TOKEN)
    service = SecretsService(secrets, audit=audit)

    result = service.status()

    by_name = {entry["name"]: entry for entry in result["secrets"]}
    assert set(by_name) == set(KNOWN_SECRETS)
    assert by_name["nox/twitch/oauth_token"]["present"] is True
    assert by_name["nox/obs/websocket_password"]["present"] is False
    assert by_name["nox/telegram/bot_token"]["group"] == "telegram"
    assert TOKEN not in repr(result)  # no value, not even truncated


async def test_unknown_names_are_refused(secrets: InMemorySecretStore, audit: FakeAudit) -> None:
    service = SecretsService(secrets, audit=audit)

    for name in ("nox/security/pin", "nox/evil/key", "not-a-name"):
        with pytest.raises(IpcError) as exc:
            await service.set(name, "x", pin=None, by="dashboard")
        assert exc.value.code == ERR_VALIDATION
        with pytest.raises(IpcError):
            await service.delete(name, pin=None, by="dashboard")
    assert secrets.names() == []


async def test_set_and_delete_a_known_secret(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    service = SecretsService(secrets, audit=audit)

    assert await service.set("nox/obs/websocket_password", TOKEN, pin=None, by="dashboard") == {
        "ok": True
    }
    assert secrets.get("nox/obs/websocket_password") == TOKEN

    assert await service.delete("nox/obs/websocket_password", pin=None, by="dashboard") == {
        "ok": True
    }
    assert secrets.get("nox/obs/websocket_password") is None


async def test_the_value_never_reaches_the_audit_log(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    await SecretsService(secrets, audit=audit).set(
        "nox/telegram/bot_token", TOKEN, pin=None, by="dashboard"
    )

    assert TOKEN not in audit.blob()
    assert audit.entries[-1]["target"] == "nox/telegram/bot_token"
    assert audit.entries[-1]["action"] == "secret.set"


async def test_a_configured_pin_gates_writes(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    pin = PinManager(secrets)
    pin.set_pin("4711")
    service = SecretsService(secrets, pin=pin, audit=audit)

    with pytest.raises(IpcError) as missing:
        await service.set("nox/twitch/client_id", "abc", pin=None, by="dashboard")
    assert missing.value.code == ERR_PERMISSION

    with pytest.raises(IpcError) as wrong:
        await service.set("nox/twitch/client_id", "abc", pin="0000", by="dashboard")
    assert wrong.value.code == ERR_PERMISSION
    assert secrets.get("nox/twitch/client_id") is None

    assert await service.set("nox/twitch/client_id", "abc", pin="4711", by="dashboard") == {
        "ok": True
    }
    assert secrets.get("nox/twitch/client_id") == "abc"


async def test_no_pin_configured_means_no_pin_required(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    service = SecretsService(secrets, pin=PinManager(secrets), audit=audit)

    assert await service.set("nox/twitch/client_id", "abc", pin=None, by="dashboard") == {
        "ok": True
    }


async def test_writes_are_rate_limited(secrets: InMemorySecretStore, audit: FakeAudit) -> None:
    ticker = Ticker()
    service = SecretsService(secrets, audit=audit, max_calls=3, window_s=60.0, clock=ticker)

    for _ in range(3):
        await service.set("nox/twitch/client_id", "abc", pin=None, by="dashboard")
    with pytest.raises(IpcError) as exc:
        await service.set("nox/twitch/client_id", "abc", pin=None, by="dashboard")
    assert exc.value.code == ERR_RATE_LIMITED

    ticker.t += 61.0
    assert await service.set("nox/twitch/client_id", "abc", pin=None, by="dashboard") == {
        "ok": True
    }


async def test_an_empty_value_is_rejected(secrets: InMemorySecretStore, audit: FakeAudit) -> None:
    with pytest.raises(IpcError) as exc:
        await SecretsService(secrets, audit=audit).set(
            "nox/twitch/client_id", "", pin=None, by="dashboard"
        )
    assert exc.value.code == ERR_VALIDATION
