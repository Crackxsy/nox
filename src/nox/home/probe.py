"""The connection test behind the Settings page's "Verbindung testen" button.

It deliberately does not go through the plugin. A user who has just typed a host and pasted a
token wants to know whether *that* works, right now - not whether a worker that was started five
minutes ago with the old values is still connected. So this is one REST request to Home
Assistant's `/api/` endpoint through the core's own egress guard, and its result says which of the
four things went wrong in words the Settings page can act on:

* `no_token` - nothing stored under `nox/home/access_token` yet,
* `blocked` - the active privacy mode or the profile allow-list refused the host,
* `unauthorized` - the host answered, and rejected the token,
* `unreachable` - nothing answered at that address.

`ok` also carries the Home Assistant version, which is the one piece of evidence that the thing
answering really is Home Assistant and not some other service on that port.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from nox.core.config import HomeConfig
from nox.core.logging import get_logger
from nox.security.egress import EgressDenied

log = get_logger(__name__)

__all__ = ["ProbeResult", "probe_home_assistant"]

#: A connection test a person is waiting for. Longer than this and "unreachable" is the answer.
PROBE_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What the Settings page needs: a verdict, a machine-readable code, and the raw detail."""

    ok: bool
    code: str
    detail: str = ""
    ha_version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "detail": self.detail,
            "ha_version": self.ha_version,
        }


def _base_url(settings: HomeConfig) -> str:
    scheme = "https" if settings.tls else "http"
    return f"{scheme}://{settings.host}:{settings.port}"


async def probe_home_assistant(
    settings: HomeConfig,
    token: str | None,
    client_factory: Callable[..., httpx.AsyncClient],
) -> ProbeResult:
    """One guarded `GET /api/` against the configured instance. Never raises for an expected
    failure."""
    if not token:
        return ProbeResult(ok=False, code="no_token")
    url = f"{_base_url(settings)}/api/"
    try:
        async with client_factory(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except EgressDenied as exc:
        log.info("home.probe_blocked", host=settings.host, port=settings.port, rule=exc.rule_id)
        return ProbeResult(ok=False, code="blocked", detail=exc.reason)
    except httpx.HTTPError as exc:
        return ProbeResult(ok=False, code="unreachable", detail=type(exc).__name__)
    if response.status_code in (401, 403):
        return ProbeResult(ok=False, code="unauthorized")
    if response.status_code != httpx.codes.OK:
        return ProbeResult(ok=False, code="http_error", detail=str(response.status_code))
    version = await _version(client_factory, settings, token)
    return ProbeResult(ok=True, code="ok", ha_version=version)


async def _version(
    client_factory: Callable[..., httpx.AsyncClient], settings: HomeConfig, token: str
) -> str:
    """Home Assistant's reported version, or `""` when `/api/config` is not readable.

    A token that may call `/api/` but not `/api/config` is unusual but possible; an empty version
    is the honest answer, not a reason to call the whole test a failure.
    """
    try:
        async with client_factory(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(
                f"{_base_url(settings)}/api/config",
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code != httpx.codes.OK:
            return ""
        payload = response.json()
    except (EgressDenied, httpx.HTTPError, ValueError):
        return ""
    return str(payload.get("version", "")) if isinstance(payload, dict) else ""
