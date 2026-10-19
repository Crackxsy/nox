"""`read_only_hosts`: a school platform may be read (GET/HEAD) but never written to.

The negative test is the POST: without the `read_only_hosts` control it would be allowed by the
ordinary allow-list, so this must deny it before any connection.
"""

from __future__ import annotations

import httpx
import pytest

from nox.security.audit import SqliteAuditLog
from nox.security.egress import EgressDenied, EgressGuard
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
    return EgressGuard(
        profile=lambda: profiles.get("coding"),
        privacy=privacy,
        audit=audit,
        global_allowlist=["*.webuntis.com:443"],
        read_only_hosts=["*.webuntis.com"],
        transport_factory=lambda: inner,
    )


async def test_get_on_read_only_host_passes(guard: EgressGuard, inner: RecordingTransport) -> None:
    async with guard.client() as client:
        r = await client.get("https://mese.webuntis.com/timetable")
        assert r.status_code == 200 and len(inner.requests) == 1


async def test_post_on_read_only_host_denied(guard: EgressGuard, inner: RecordingTransport) -> None:
    async with guard.client() as client:
        with pytest.raises(EgressDenied) as exc:
            await client.post("https://mese.webuntis.com/timetable", json={"x": 1})
        assert exc.value.rule_id == "read_only_host"
        assert not inner.requests  # the write never reached the transport


def test_is_read_only_host(guard: EgressGuard) -> None:
    assert guard.is_read_only_host("mese.webuntis.com")
    assert not guard.is_read_only_host("example.com")
