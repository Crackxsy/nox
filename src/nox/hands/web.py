"""`web.fetch` and `web.search`: read a public page, or search, never the LAN.

`web.fetch` gets any public `http(s)` URL with GET, follows redirects by hand so every hop is
re-checked, caps the body at 2 MiB and hands back extracted text. The one hard rule, enforced below
the model by `NetGuard`: the URL's host is resolved and every address it points at is judged, so a
name that resolves to loopback, a `169.254` metadata address or the home LAN is refused - and
because the check is on the resolved address and repeated on each redirect, a DNS-rebind or a
redirect to `http://169.254.169.254/` is caught too. A host on `read_only_hosts` (school platforms)
may only be read, which GET already is; the guard refuses anything else.

`web.search` queries a SearXNG instance (its base URL configured, e.g. on the Pi) and returns the
top results. That instance usually lives on the LAN, which `NetGuard` refuses - so the search
client is the one exception, and a narrow one (`SearchBackendTransport`): it reaches exactly the
origin (scheme, host, port) the *user* configured in `hands.searxng_base_url`, nothing else, never
a link-local/metadata address and never the Home Assistant API port. The model supplies only the
query; it can neither name the backend nor follow a redirect away from it. No DuckDuckGo
scraping; Brave is a possible later backend behind a key in the secret store. Claude's own web
search already serves FULL/BALANCED turns via the provider.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from pydantic import BaseModel

from nox.core.logging import get_logger
from nox.sandbox.netguard import BlockedAddress, NetGuard, resolve_all
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = [
    "PinnedTransport",
    "WebSettings",
    "extract_text",
    "SearchBackendTransport",
    "guarded_web_client",
    "origin_of",
    "register_web_tools",
    "safe_url",
    "search_backend_client",
]

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n\s*\n+")


@dataclass(frozen=True, slots=True)
class WebSettings:
    enabled: bool = True
    searxng_base_url: str = ""
    read_only_hosts: tuple[str, ...] = ("*.webuntis.com",)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """Resolve, check and *dial the same IP* - closing the DNS-rebinding window.

    Without this, `safe_url` resolved a hostname, judged the address, and then httpx resolved the
    name a second time to connect: between the two a DNS answer can flip to the LAN (TOCTOU). Here
    the check happens inside the transport, on the address that is about to be dialed: the request
    URL is rewritten to the vetted IP, while the `Host` header and the TLS SNI/verification stay the
    original hostname (`sni_hostname` extension). Every redirect hop goes through this same path, so
    each hop is re-checked against what it actually connects to.
    """

    def __init__(self, guard: NetGuard, **kwargs: Any) -> None:
        from nox.security.egress import shared_ssl_context

        super().__init__(verify=shared_ssl_context(), **kwargs)
        self._guard = guard

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        addresses = resolve_all(host, port)  # the one resolution that is both judged and dialed
        self._guard.check_addresses(host, addresses)  # raises BlockedAddress
        ip = str(addresses[0])
        request.url = request.url.copy_with(host=ip)
        request.headers["host"] = f"{host}:{port}" if port not in (80, 443) else host
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await super().handle_async_request(request)


Origin = tuple[str, str, int]


def origin_of(url: str) -> Origin:
    """(scheme, lower-case host, port) of an http(s) URL; `ValueError` for anything else."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the search backend must be an http(s) URL with a host")
    return (
        parts.scheme,
        parts.hostname.lower().rstrip("."),
        parts.port or (443 if parts.scheme == "https" else 80),
    )


class SearchBackendTransport(httpx.AsyncHTTPTransport):
    """Reaches exactly one origin - the user-configured search backend - even on the LAN.

    Everything else about `PinnedTransport` holds: the address is resolved once, judged, and
    dialed. What differs is the judgement: a private/CGNAT/loopback address is allowed *for this
    origin only*, because the user put it there; link-local (cloud metadata), multicast and the
    unspecified address never are, nor is the Home Assistant host on its API port.
    """

    def __init__(
        self,
        base_url: str,
        guard: NetGuard,
        *,
        home_assistant_port: int | None = None,
        **kwargs: Any,
    ) -> None:
        from nox.security.egress import shared_ssl_context

        super().__init__(verify=shared_ssl_context(), **kwargs)
        self._origin = origin_of(base_url)
        self._guard = guard
        self._ha_port = home_assistant_port

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        scheme, host, port = origin_of(str(request.url))
        if (scheme, host, port) != self._origin:
            raise BlockedAddress(host, "not the configured search backend")
        addresses = resolve_all(host, port)
        if not addresses:
            raise BlockedAddress(host, "does not resolve to any address")
        for ip in addresses:
            if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
                raise BlockedAddress(host, "link-local, multicast or reserved address")
            is_ha = str(ip) in self._guard.home_assistant_ips or host in self._guard.blocked_hosts
            if is_ha and (self._ha_port is None or port == self._ha_port):
                raise BlockedAddress(host, "the Home Assistant host is never reachable this way")
        request.url = request.url.copy_with(host=str(addresses[0]))
        request.headers["host"] = f"{host}:{port}" if port not in (80, 443) else host
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await super().handle_async_request(request)


def search_backend_client(
    base_url: str,
    guard: NetGuard,
    *,
    home_assistant_port: int | None = None,
    timeout: float = 20.0,
) -> httpx.AsyncClient:
    """A client for the configured search backend and nothing else."""
    transport = SearchBackendTransport(base_url, guard, home_assistant_port=home_assistant_port)
    return httpx.AsyncClient(transport=transport, timeout=timeout)


def guarded_web_client(guard: NetGuard, *, timeout: float = 20.0) -> httpx.AsyncClient:
    """An `AsyncClient` that may reach any public host but never the LAN, with the IP pinned."""
    return httpx.AsyncClient(transport=PinnedTransport(guard), timeout=timeout)


def safe_url(url: str, guard: NetGuard) -> tuple[str, int]:
    """Validate the scheme and reject a host that resolves anywhere private. Returns (host, port).

    A fail-fast pre-check; `PinnedTransport` is the authoritative dial-time check. Raises
    `BlockedAddress` (host unreachable by policy) or `ValueError` (not an http(s) URL).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"only http(s) URLs are fetched, not {parts.scheme!r}")
    host = parts.hostname or ""
    if not host:
        raise ValueError("URL has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    guard.check_addresses(host, resolve_all(host, port))
    return host, port


def extract_text(html: str, *, limit: int = 20000) -> str:
    """Crude but dependency-free text extraction: drop script/style and tags, collapse space."""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = _TAG.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()[:limit]


def _is_read_only(host: str, patterns: tuple[str, ...]) -> bool:
    from nox.security.egress import entry_matches

    host_l = host.strip().lower().rstrip(".")
    return any(entry_matches(entry, host_l, 443) for entry in patterns)


ClientFactory = Callable[[], httpx.AsyncClient]
#: Builds the client for one search backend base URL (from configuration, never from the model).
SearchClientFactory = Callable[[str], httpx.AsyncClient]
WebPolicy = Callable[[], WebSettings]


async def _fetch(
    url: str, *, guard: NetGuard, client: httpx.AsyncClient, read_only: tuple[str, ...]
) -> dict[str, Any]:
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        host, _port = safe_url(current, guard)  # raises on a private/blocked address
        if _is_read_only(host, read_only):
            pass  # GET is allowed on read-only hosts; this is the point where a write would be cut
        response = await client.get(current, follow_redirects=False)
        if response.is_redirect:
            location = response.headers.get("location", "")
            if not location:
                break
            current = urljoin(current, location)
            continue
        body = response.content[:MAX_BODY_BYTES]
        truncated = len(response.content) > MAX_BODY_BYTES
        content_type = response.headers.get("content-type", "")
        text = extract_text(body.decode(response.encoding or "utf-8", errors="replace"))
        return {
            "ok": response.status_code < 400,
            "status": response.status_code,
            "url": str(response.url),
            "content_type": content_type,
            "text": text,
            "truncated": truncated,
        }
    return {"ok": False, "error": "web.too_many_redirects", "url": current}


def register_web_tools(
    registry: ToolRegistry,
    *,
    guard: NetGuard,
    client_factory: ClientFactory,
    settings: WebPolicy,
    search_client_factory: SearchClientFactory | None = None,
) -> None:
    """Register `web.fetch` and `web.search`. Both run below the privacy engine's cloud gate."""

    class FetchInput(BaseModel):
        url: str

    class SearchInput(BaseModel):
        query: str
        count: int = 5

    async def fetch(payload: dict[str, Any]) -> dict[str, Any]:
        cfg = settings()
        if not cfg.enabled:
            return {"ok": False, "error": "web.disabled"}
        model = FetchInput.model_validate(payload)
        async with client_factory() as client:
            try:
                return await _fetch(
                    model.url, guard=guard, client=client, read_only=cfg.read_only_hosts
                )
            except BlockedAddress as blocked:
                return {"ok": False, "error": "web.blocked", "detail": blocked.reason}
            except ValueError as exc:
                return {"ok": False, "error": "web.bad_url", "detail": str(exc)}
            except httpx.HTTPError as exc:
                return {"ok": False, "error": "web.http_error", "detail": type(exc).__name__}

    async def search(payload: dict[str, Any]) -> dict[str, Any]:
        cfg = settings()
        if not cfg.enabled:
            return {"ok": False, "error": "web.disabled"}
        if not cfg.searxng_base_url:
            return {
                "ok": False,
                "error": "web.search_unconfigured",
                "detail": "no SearXNG base URL is configured (web.searxng_base_url)",
            }
        model = SearchInput.model_validate(payload)
        base = cfg.searxng_base_url.rstrip("/")
        query = urlencode({"q": model.query, "format": "json"})
        try:
            origin_of(base)
        except ValueError as exc:
            return {"ok": False, "error": "web.search_unconfigured", "detail": str(exc)}
        make_client = search_client_factory or (lambda url: search_backend_client(url, guard))
        async with make_client(base) as client:
            try:
                response = await client.get(f"{base}/search?{query}", follow_redirects=False)
                data = response.json()
            except BlockedAddress as blocked:
                return {"ok": False, "error": "web.blocked", "detail": blocked.reason}
            except (httpx.HTTPError, ValueError) as exc:
                return {"ok": False, "error": "web.search_failed", "detail": type(exc).__name__}
        results = [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in (data.get("results") or [])[: model.count]
        ]
        return {"ok": True, "results": results}

    registry.register(
        ToolSpec(
            name="web.fetch",
            description="Fetch a public web page as text.",
            input_model=FetchInput,
            risk=Risk.LOW,
            handler=fetch,
            side_effects=False,
            local=False,
            targets=lambda p: str(p.get("url", "")),
            network=True,
            lookup=lambda p: str(p.get("url", "")),
        )
    )
    registry.register(
        ToolSpec(
            name="web.search",
            description="Search the web in the user's own words; returns the top results.",
            input_model=SearchInput,
            risk=Risk.LOW,
            handler=search,
            side_effects=False,
            local=False,
            targets=lambda p: str(p.get("query", "")),
            network=True,
            lookup=lambda p: str(p.get("query", "")),
        )
    )
