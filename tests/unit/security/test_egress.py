"""Egress guard: OFFLINE/PRIVATE allow only allow-listed loopback services (OP-7 C), allow-lists,
no bypass via client kwargs."""

from __future__ import annotations

import httpx
import pytest

from nox.core.state import PrivacyMode
from nox.security.audit import SqliteAuditLog
from nox.security.egress import (
    EgressDenied,
    EgressGuard,
    entry_matches,
    is_loopback,
    loopback_entry_matches,
)
from nox.security.model import Profile
from nox.security.privacy import PrivacyService
from nox.security.profiles import YamlProfileProvider


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def inner() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def guard(
    profiles: YamlProfileProvider,
    privacy: PrivacyService,
    audit: SqliteAuditLog,
    inner: RecordingTransport,
) -> EgressGuard:
    # `coding`, not `companion`: these tests are about the *inheritance* rule ("a profile with
    # an empty egress_allowlist falls back to the global one"), and since EPIC-17 `companion`
    # carries its own single entry (api.telegram.org:443) and therefore no longer inherits.
    # `coding` is the remaining cloud_allowed profile with an empty list.
    holder = {"profile": profiles.get("coding")}
    g = EgressGuard(
        profile=lambda: holder["profile"],
        privacy=privacy,
        audit=audit,
        global_allowlist=["api.anthropic.com:443", "*.googleapis.com", "example.org:8080"],
        loopback_allowlist=["127.0.0.1:11434"],
        transport_factory=lambda: inner,
    )
    g._holder = holder  # type: ignore[attr-defined]
    return g


async def test_private_mode_denies_cloud_egress_before_any_connection(
    guard: EgressGuard,
    privacy: PrivacyService,
    inner: RecordingTransport,
) -> None:
    async with guard.client() as client:
        r = await client.get("https://api.anthropic.com/v1/messages")
        assert r.status_code == 200 and len(inner.requests) == 1
        await privacy.set_mode(PrivacyMode.PRIVATE)
        with pytest.raises(EgressDenied) as exc:
            await client.get("https://api.anthropic.com/v1/messages")
        assert exc.value.rule_id == "privacy.private" and exc.value.port == 443
        assert len(inner.requests) == 1  # inner transport never touched
        r = await client.get("http://127.0.0.1:11434/api/tags")  # allow-listed loopback stays fine
        assert r.status_code == 200 and len(inner.requests) == 2
        with pytest.raises(EgressDenied, match="privacy.private.loopback_denied"):
            await client.get("http://127.0.0.1:47800/")  # loopback, but not allow-listed
        assert len(inner.requests) == 2


async def test_offline_mode_allows_only_allowlisted_loopback_services(
    guard: EgressGuard,
    privacy: PrivacyService,
    audit: SqliteAuditLog,
    inner: RecordingTransport,
) -> None:
    """OP-7 C: the decided offline baseline (local chat via Ollama) must work; nothing else does."""
    await privacy.set_mode(PrivacyMode.OFFLINE)
    async with guard.client() as client:
        r = await client.get("http://127.0.0.1:11434/api/tags")  # Ollama is allow-listed
        assert r.status_code == 200 and len(inner.requests) == 1
        r = await client.get("http://localhost:11434/api/tags")  # localhost == 127.0.0.1
        assert r.status_code == 200 and len(inner.requests) == 2
        for url, rule in (
            ("http://localhost:47800/", "privacy.offline.loopback_denied"),
            ("http://127.0.0.1:4455/", "privacy.offline.loopback_denied"),
            ("https://example.org", "privacy.offline"),
        ):
            with pytest.raises(EgressDenied) as exc:
                await client.get(url)
            assert exc.value.rule_id == rule
    assert len(inner.requests) == 2  # inner transport never saw a denied request
    rows = [e for e in audit.entries() if e.actor == "egress"]
    assert [r.result for r in rows] == ["ok", "ok", "denied", "denied", "denied"]
    assert audit.details(rows[2].seq)["rule_id"] == "privacy.offline.loopback_denied"


async def test_offline_profile_allows_ollama_and_stream_profile_adds_obs(
    guard: EgressGuard, profiles: YamlProfileProvider, privacy: PrivacyService
) -> None:
    """The profile's loopback_allowlist is additive to the global one (OP-7 C)."""
    await privacy.set_mode(PrivacyMode.OFFLINE)
    guard._holder["profile"] = profiles.get("offline")  # type: ignore[attr-defined]
    assert guard.check("127.0.0.1", 11434).allowed
    assert not guard.check("127.0.0.1", 4455).allowed
    guard._holder["profile"] = profiles.get("stream")  # type: ignore[attr-defined]
    assert guard.check("127.0.0.1", 4455).allowed  # OBS websocket
    assert guard.check("127.0.0.1", 11434).allowed
    assert not guard.check("127.0.0.1", 9999).allowed
    assert not guard.check("api.anthropic.com", 443).allowed


def test_loopback_entry_matching_treats_localhost_as_127_0_0_1() -> None:
    assert loopback_entry_matches("127.0.0.1:11434", "localhost", 11434)
    assert loopback_entry_matches("localhost:11434", "127.0.0.1", 11434)
    assert loopback_entry_matches("[::1]:11434", "::1", 11434)
    assert loopback_entry_matches("127.0.0.1:*", "localhost", 1)
    assert not loopback_entry_matches("127.0.0.1:11434", "127.0.0.1", 11435)
    assert not loopback_entry_matches("127.0.0.1:11434", "127.0.0.2", 11434)
    assert not loopback_entry_matches("none", "127.0.0.1", 11434)


def test_allowlist_matching_and_default_deny(guard: EgressGuard) -> None:
    assert guard.check("api.anthropic.com", 443).allowed
    assert not guard.check("api.anthropic.com", 80).allowed
    assert guard.check("oauth2.googleapis.com", 443).allowed
    assert guard.check("example.org", 8080).allowed
    d = guard.check("evil.example.com", 443)
    assert not d.allowed and d.rule_id == "global.default_deny"
    assert guard.check("LOCALHOST", 47800).allowed and guard.check("::1", 1).allowed
    assert is_loopback("127.5.5.5") and not is_loopback("10.0.0.1")
    assert entry_matches("https://api.example.com", "api.example.com", 443)
    assert entry_matches("host:*", "host", 1234) and not entry_matches("none", "none", 1)


def test_work_profile_uses_only_its_own_list(
    guard: EgressGuard, profiles: YamlProfileProvider
) -> None:
    guard._holder["profile"] = profiles.get("work")  # type: ignore[attr-defined]
    d = guard.check("api.anthropic.com", 443)
    assert not d.allowed and d.rule_id == "profile.work.default_deny"
    assert guard.check("127.0.0.1", 11434).allowed


def test_offline_profile_and_none_entry(guard: EgressGuard, profiles: YamlProfileProvider) -> None:
    guard._holder["profile"] = profiles.get("offline")  # type: ignore[attr-defined]
    d = guard.check("api.anthropic.com", 443)
    assert not d.allowed and d.rule_id == "profile.offline.default_deny"
    custom = Profile(id="c", rules=[], cloud_allowed=True, egress_allowlist=["none", "api.x.com"])
    guard._holder["profile"] = custom  # type: ignore[attr-defined]
    assert not guard.check("api.x.com", 443).allowed


async def test_denied_and_allowed_requests_are_audited_without_paths(
    guard: EgressGuard,
    audit: SqliteAuditLog,
    privacy: PrivacyService,
) -> None:
    async with guard.client() as client:
        await client.get("https://api.anthropic.com/v1/secret-path?token=abc")
        await privacy.set_mode(PrivacyMode.PRIVATE)
        with pytest.raises(EgressDenied):
            await client.get("https://api.anthropic.com/v1/x")
    rows = [e for e in audit.entries() if e.actor == "egress"]
    assert [r.result for r in rows] == ["ok", "denied"]
    assert all("secret-path" not in r.target and "token" not in r.target for r in rows)
    assert rows[0].target == "https://api.anthropic.com:443"


def test_client_refuses_transport_bypass(guard: EgressGuard) -> None:
    for key in ("transport", "mounts", "proxy"):
        with pytest.raises(ValueError, match=key):
            guard.client(**{key: object()})
