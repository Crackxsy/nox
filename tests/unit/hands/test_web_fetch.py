"""`web.fetch`: public pages only, never the LAN - including via a redirect (SSRF/rebind)."""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from nox.hands import web
from nox.sandbox.netguard import BlockedAddress, NetGuard


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve names deterministically: *.lan -> private, everything else -> a public address."""

    def fake_resolve(host: str, port: int) -> list:
        if host.endswith(".lan") or host == "metadata":
            return [
                ipaddress.ip_address("169.254.169.254" if host == "metadata" else "192.168.0.5")
            ]
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(web, "resolve_all", fake_resolve)


def test_safe_url_rejects_non_http() -> None:
    with pytest.raises(ValueError, match="http"):
        web.safe_url("file:///etc/passwd", NetGuard())


def test_safe_url_rejects_lan() -> None:
    with pytest.raises(BlockedAddress):
        web.safe_url("http://intranet.lan/", NetGuard())


def test_safe_url_allows_public() -> None:
    host, port = web.safe_url("https://example.com/page", NetGuard())
    assert host == "example.com" and port == 443


def test_extract_text_strips_tags_and_scripts() -> None:
    html = "<html><head><style>x{}</style></head><body><p>Hello</p><script>evil()</script></body>"
    text = web.extract_text(html)
    assert "Hello" in text and "evil" not in text and "<p>" not in text


async def test_fetch_follows_redirect_but_blocks_rebind_to_metadata() -> None:
    guard = NetGuard()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://metadata/latest/"})
        return httpx.Response(200, text="secret")

    transport = httpx.MockTransport(handler)
    # The redirect target resolves to the metadata address; `_fetch` re-checks each hop and the
    # guard refuses it (the tool handler turns this into a `web.blocked` error for the model).
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BlockedAddress):
            await web._fetch("https://example.com/start", guard=guard, client=client, read_only=())


async def test_fetch_returns_text_for_a_public_page() -> None:
    guard = NetGuard()
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<p>hi there</p>"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await web._fetch("https://example.com/", guard=guard, client=client, read_only=())
    assert result["ok"] is True and "hi there" in result["text"]


async def test_pinned_transport_checks_the_ip_it_dials_not_the_first_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOCTOU: the resolver flips to the LAN on the dial. The transport checks that address (the
    one it connects to), so the connection is refused - not the earlier, public answer."""
    calls = {"n": 0}

    def flipping(host: str, port: int) -> list:
        calls["n"] += 1
        # First answer (any pre-check) public, the dial-time answer private.
        if calls["n"] == 1:
            return [ipaddress.ip_address("93.184.216.34")]
        return [ipaddress.ip_address("192.168.0.5")]

    monkeypatch.setattr(web, "resolve_all", flipping)
    transport = web.PinnedTransport(NetGuard())

    request = httpx.Request("GET", "https://example.com/")
    # Drive the transport straight so the dial-time check is what we observe.
    web.resolve_all(  # consume the first (public) answer, as a pre-check would
        "example.com", 443
    )
    with pytest.raises(BlockedAddress):
        await transport.handle_async_request(request)
    await transport.aclose()


async def test_pinned_transport_dials_the_ip_and_keeps_sni_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The socket is opened to the vetted IP, while Host and TLS SNI stay the original hostname."""
    seen: dict[str, object] = {}

    async def fake_super(self, request):  # noqa: ANN001, ANN202 - stubs the real network call
        seen["host"] = request.url.host
        seen["sni"] = request.extensions.get("sni_hostname")
        seen["hdr"] = request.headers.get("host")
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(web, "resolve_all", lambda h, p: [ipaddress.ip_address("93.184.216.34")])
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_super)
    transport = web.PinnedTransport(NetGuard())
    await transport.handle_async_request(httpx.Request("GET", "https://example.com/p"))
    assert seen["host"] == "93.184.216.34"  # dialed the vetted IP, not the name
    assert seen["sni"] == "example.com"  # SNI / cert verification keep the hostname
    assert str(seen["hdr"]).startswith("example.com")


# ---- web.search backend exception (review finding 16) ------------------------------------------


def _search_tool(base_url: str, captured: list[str]) -> object:
    """`web.search` wired like the core does, with the network stubbed at the transport."""
    from nox.tools.registry import ToolRegistry

    async def fake_super(self, request):  # noqa: ANN001, ANN202 - stubs the real network call
        captured.append(f"{request.url.host}|{request.headers.get('host')}")
        return httpx.Response(200, json={"results": [{"title": "t", "url": "u", "content": "c"}]})

    registry = ToolRegistry()
    web.register_web_tools(
        registry,
        guard=NetGuard(),
        client_factory=lambda: web.guarded_web_client(NetGuard()),
        settings=lambda: web.WebSettings(searxng_base_url=base_url),
        search_client_factory=lambda base: web.search_backend_client(base, NetGuard()),
    )
    return registry.get("web.search"), fake_super


async def test_search_reaches_the_configured_lan_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SearXNG instance on the Pi resolves to the LAN; NetGuard alone refused it.
    captured: list[str] = []
    spec, fake_super = _search_tool("http://searx.lan:8080", captured)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_super)
    result = await spec.handler({"query": "wetter"})  # type: ignore[attr-defined]
    assert result["ok"] is True, result
    assert captured == ["192.168.0.5|searx.lan:8080"]


async def test_search_backend_transport_refuses_any_other_origin() -> None:
    transport = web.SearchBackendTransport("http://searx.lan:8080", NetGuard())
    for url in (
        "http://other.lan:8080/search",  # another LAN host
        "http://searx.lan:8123/api/states",  # same host, other port
        "https://searx.lan:8080/search",  # other scheme
        "http://example.com/x",  # the internet
    ):
        with pytest.raises(BlockedAddress, match="configured search backend"):
            await transport.handle_async_request(httpx.Request("GET", url))
    await transport.aclose()


async def test_search_backend_never_reaches_metadata_or_home_assistant() -> None:
    meta = web.SearchBackendTransport("http://metadata:80", NetGuard())
    with pytest.raises(BlockedAddress, match="link-local"):
        await meta.handle_async_request(httpx.Request("GET", "http://metadata/search"))
    guard = NetGuard(home_assistant_ips=frozenset({"192.168.0.5"}))
    ha = web.SearchBackendTransport("http://searx.lan:8123", guard, home_assistant_port=8123)
    with pytest.raises(BlockedAddress, match="Home Assistant"):
        await ha.handle_async_request(httpx.Request("GET", "http://searx.lan:8123/search"))
    await meta.aclose()
    await ha.aclose()


def test_web_fetch_still_refuses_the_lan() -> None:
    # The exception is for the configured search backend only, never for web.fetch.
    with pytest.raises(BlockedAddress):
        web.safe_url("http://searx.lan:8080/", NetGuard())
