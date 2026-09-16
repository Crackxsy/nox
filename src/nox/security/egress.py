"""Egress guard: the only way to obtain an `httpx.AsyncClient` in the core (Security Model §5,
FR-14.2).

Every request passes `GuardedTransport`, which checks host:port against the privacy mode and the
active profile's allow-list and raises `EgressDenied` before any connection is made. Every attempt
is audited (host and port only, no path).

- OFFLINE and PRIVATE (OP-7 C): only loopback services on the merged loopback allow-list
  (`security.loopback_allowlist` plus the active profile's additive `loopback_allowlist`) pass;
  everything else, loopback included, is denied and audited.
- FULL and BALANCED: loopback passes, other hosts need the active profile's `egress_allowlist`
  (empty and `cloud_allowed: true` -> the global `security.egress_allowlist`; `cloud_allowed:
  false` -> only the profile's own list; the entry "none" blocks everything).
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Sequence
from fnmatch import fnmatchcase
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from nox.core.state import PrivacyMode
from nox.security._logging import get_logger
from nox.security.model import AuditLog, Profile

log = get_logger(__name__)

LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"})
NO_EGRESS = "none"
_FORBIDDEN_CLIENT_KWARGS = ("transport", "mounts", "proxy", "proxies")

_SSL_CONTEXT: ssl.SSLContext | None = None


def shared_ssl_context() -> ssl.SSLContext:
    """One verified SSL context per process.

    `httpx.AsyncHTTPTransport()` builds a fresh context and loads the CA bundle on every
    construction - about 250 ms of synchronous file I/O on Windows. Providers open one client per
    request, so the default transport factory reuses this context; otherwise a vault scan with one
    embedding request per chunk starved the event loop (heartbeats and IPC handshakes timed out,
    2026-09-15)."""
    global _SSL_CONTEXT  # noqa: PLW0603 - process-wide cache by design
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = httpx.create_ssl_context()
    return _SSL_CONTEXT


def default_transport() -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(verify=shared_ssl_context())


class EgressDenied(PermissionError):  # noqa: N818 - name fixed by the Security Model
    def __init__(self, host: str, port: int, rule_id: str, reason: str) -> None:
        super().__init__(f"egress to {host}:{port} denied ({rule_id}): {reason}")
        self.host = host
        self.port = port
        self.rule_id = rule_id
        self.reason = reason


class EgressDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    allowed: bool
    rule_id: str
    reason: str = ""


class PrivacyModeSource(Protocol):
    @property
    def mode(self) -> PrivacyMode: ...


def is_loopback(host: str) -> bool:
    h = host.strip().lower().strip("[]")
    return h in LOOPBACK_HOSTS or h.startswith("127.")


def normalize_loopback_host(host: str) -> str:
    """`localhost`/`::1` and friends denote the same machine-local address as `127.0.0.1`."""
    h = host.strip().lower().strip("[]").rstrip(".")
    return "127.0.0.1" if h in LOOPBACK_HOSTS else h


def _split_entry(entry: str) -> tuple[str, str] | None:
    """`host` / `host:port` / `scheme://host:port` -> (host, port or "*"); None for empty/"none"."""
    text = entry.strip().lower()
    if not text or text == NO_EGRESS:
        return None
    if "://" in text:
        text = text.split("://", 1)[1].split("/", 1)[0]
    host_part, sep, port_part = text.rpartition(":")
    if not sep or not host_part or not (port_part.isdigit() or port_part == "*"):
        return text, "*"
    return host_part, port_part


def entry_matches(entry: str, host: str, port: int) -> bool:
    """Allow-list entries: `host`, `host:port`, `*.example.com:443`, `host:*`."""
    parts = _split_entry(entry)
    if parts is None:
        return False
    host_part, port_part = parts
    if not fnmatchcase(host.lower(), host_part):
        return False
    return port_part == "*" or int(port_part) == port


def loopback_entry_matches(entry: str, host: str, port: int) -> bool:
    """Like `entry_matches`, but `localhost`, `::1` and `127.0.0.1` are the same host."""
    parts = _split_entry(entry)
    if parts is None:
        return False
    host_part, port_part = parts
    if normalize_loopback_host(host_part) != normalize_loopback_host(host):
        return False
    return port_part == "*" or int(port_part) == port


class EgressGuard:
    def __init__(
        self,
        *,
        profile: Callable[[], Profile],
        privacy: PrivacyModeSource,
        audit: AuditLog | None = None,
        global_allowlist: Sequence[str] = (),
        loopback_allowlist: Sequence[str] = (),
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._profile = profile
        self._privacy = privacy
        self._audit = audit
        self._global_allowlist = tuple(global_allowlist)
        self._loopback_allowlist = tuple(loopback_allowlist)
        self._transport_factory = transport_factory or default_transport

    def loopback_allowlist(self) -> tuple[str, ...]:
        """Global loopback allow-list plus the active profile's additive entries (OP-7 C)."""
        return self._loopback_allowlist + tuple(self._profile().loopback_allowlist)

    def _check_loopback_allowlist(self, host: str, port: int, rule_id: str) -> EgressDecision:
        """PRIVATE/OFFLINE: a loopback service passes only when it is explicitly allow-listed."""
        for entry in self.loopback_allowlist():
            if loopback_entry_matches(entry, host, port):
                return EgressDecision(
                    allowed=True, rule_id=f"{rule_id}.loopback_allowlist", reason=entry
                )
        return EgressDecision(
            allowed=False,
            rule_id=f"{rule_id}.loopback_denied",
            reason="loopback service not on the loopback allow-list",
        )

    def check(self, host: str, port: int) -> EgressDecision:
        host_l = host.strip().lower().rstrip(".")
        mode = self._privacy.mode
        loopback = is_loopback(host_l)
        if mode is PrivacyMode.OFFLINE:
            if loopback:
                return self._check_loopback_allowlist(host_l, port, "privacy.offline")
            return EgressDecision(
                allowed=False,
                rule_id="privacy.offline",
                reason="OFFLINE: only allow-listed loopback services",
            )
        if mode is PrivacyMode.PRIVATE:
            if loopback:
                return self._check_loopback_allowlist(host_l, port, "privacy.private")
            return EgressDecision(
                allowed=False,
                rule_id="privacy.private",
                reason="PRIVATE: only allow-listed loopback services",
            )
        if loopback:
            return EgressDecision(allowed=True, rule_id="loopback")
        profile = self._profile()
        if profile.egress_allowlist or not profile.cloud_allowed:
            allowlist: Sequence[str] = profile.egress_allowlist
            source = f"profile.{profile.id}"
        else:
            allowlist = self._global_allowlist
            source = "global"
        if not allowlist:
            return EgressDecision(
                allowed=False,
                rule_id=f"{source}.default_deny",
                reason="no allow-list entry (network is default-deny)",
            )
        if any(e.strip().lower() == NO_EGRESS for e in allowlist):
            return EgressDecision(
                allowed=False, rule_id=f"{source}.none", reason="profile forbids egress"
            )
        for entry in allowlist:
            if entry_matches(entry, host_l, port):
                return EgressDecision(allowed=True, rule_id=f"{source}.allowlist", reason=entry)
        return EgressDecision(
            allowed=False,
            rule_id=f"{source}.default_deny",
            reason="host:port not on the allow-list",
        )

    def authorize(self, host: str, port: int, *, scheme: str = "https", method: str = "") -> None:
        """Check and audit; raises EgressDenied."""
        decision = self.check(host, port)
        if self._audit is not None:
            self._audit.append(
                actor="egress",
                tool="network",
                action="request",
                target=f"{scheme}://{host}:{port}",
                decision="allow" if decision.allowed else "deny",
                result="ok" if decision.allowed else "denied",
                details={"rule_id": decision.rule_id, "method": method},
            )
        if not decision.allowed:
            log.warning("egress.denied", host=host, port=port, rule_id=decision.rule_id)
            raise EgressDenied(host, port, decision.rule_id, decision.reason)

    def transport(self, inner: httpx.AsyncBaseTransport | None = None) -> GuardedTransport:
        return GuardedTransport(self, inner or self._transport_factory())

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        """An AsyncClient whose every request is guarded; transport/mounts/proxy kwargs refused."""
        for key in _FORBIDDEN_CLIENT_KWARGS:
            if key in kwargs:
                raise ValueError(
                    f"EgressGuard.client() does not accept {key!r}; the guarded transport is fixed"
                )
        return httpx.AsyncClient(transport=self.transport(), **kwargs)


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, guard: EgressGuard, inner: httpx.AsyncBaseTransport) -> None:
        self._guard = guard
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        port = url.port or (443 if url.scheme == "https" else 80)
        self._guard.authorize(url.host, port, scheme=url.scheme, method=request.method)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
