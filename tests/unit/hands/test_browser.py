"""browser.*: Nox's headless Chromium reaches public pages and never the LAN.

The pure parts (origin normalisation, the route check, the proxy's plain-HTTP forwarding) run
everywhere. The end-to-end tests drive a real *headless* Chromium against a local fake web server
that the test resolver presents as a public host: every connection the proxy approves is dialed
to that loopback server, so a request that should have been blocked would show up in the server's
log with its forbidden Host. They skip when Playwright's Chromium is not installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from nox.hands import browser as browser_mod
from nox.hands.browser import (
    BrowserSession,
    BrowserSettings,
    BrowserUnavailable,
    judge_request,
    page_origin,
    register_browser_tools,
)
from nox.sandbox.netguard import NetGuard
from nox.sandbox.proxy import EgressProxy
from nox.security.model import Decision, Profile, Risk
from nox.security.permissions import DefaultPermissionEngine, PrivacySnapshot
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry

PUBLIC_IP = "93.184.216.34"
NAMES = {"public.test": PUBLIC_IP, "other.test": "93.184.216.35", "lan.test": "192.168.1.5"}


def fake_resolver(host: str, _port: int) -> list[Any]:
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    if host not in NAMES:
        raise OSError(f"unknown host {host}")
    return [ipaddress.ip_address(NAMES[host])]


# ---- pure parts ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Shop.Example/a?b", "https://shop.example"),
        ("https://shop.example:443/", "https://shop.example"),
        ("http://shop.example:8080/x", "http://shop.example:8080"),
        ("http://[::1]:80/", "http://[::1]"),
        ("file:///etc/passwd", ""),
        ("javascript:alert(1)", ""),
        ("https://shop.example:99999/", ""),
        ("", ""),
    ],
)
def test_page_origin(url: str, origin: str) -> None:
    assert page_origin(url) == origin


@pytest.mark.parametrize(
    ("url", "navigation", "blocked"),
    [
        ("http://public.test/", True, False),
        ("http://lan.test/", True, True),  # resolves to the LAN
        ("http://lan.test/x.png", False, False),  # subresource: the proxy judges the address
        ("http://192.168.0.1/admin", False, True),  # a literal private address, even a subresource
        ("http://169.254.169.254/latest/meta-data", True, True),
        ("http://[::1]/", False, True),
        ("http://localhost:8080/", False, True),
        ("http://api.localhost/", True, True),
        ("http://ha.local/", True, True),  # the Home Assistant host, by name
        ("file:///C:/Windows/win.ini", True, True),
        ("chrome://settings", True, True),
        ("ftp://public.test/", True, True),
        ("data:text/html,hi", True, False),
        ("about:blank", True, False),
        ("http://unknown.test/", True, True),  # unresolvable: refused, not guessed
    ],
)
async def test_judge_request(url: str, navigation: bool, blocked: bool) -> None:
    guard = NetGuard(blocked_hosts=frozenset({"ha.local"}))
    reason = await judge_request(url, navigation=navigation, guard=guard, resolver=fake_resolver)
    assert (reason is not None) is blocked, reason


# ---- a fake web: what the proxy dials when it approves a connection ------------------------------


class FakeWeb:
    """Loopback HTTP server; records (method, Host header, path, body) of every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str, str]] = []
        web = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def _send(self, code: int, body: str, headers: dict[str, str] | None = None) -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                web.requests.append(("GET", self.headers.get("Host", ""), self.path, ""))
                pages = {
                    "/": "<title>Fahrplan</title><h1>RB 40</h1><p>07:12 heute 12 Min spaeter</p>"
                    '<a href="/form">Formular</a>',
                    "/form": "<title>Form</title><form method=post action=/send>"
                    "<input id=q name=q><button id=go>Go</button></form>",
                    "/img-lan": '<title>Img</title><img src="http://10.0.0.7/pixel.png">',
                    "/script-lan": "<title>Script</title><script>"
                    "fetch('http://lan.test/steal').catch(() => {});</script>",
                }
                if self.path in pages:
                    self._send(200, pages[self.path])
                elif self.path == "/redirect-lan":
                    self._send(302, "", {"Location": "http://192.168.0.1/admin"})
                elif self.path == "/redirect-lan-name":
                    self._send(302, "", {"Location": "http://lan.test/admin"})
                elif self.path == "/redirect-metadata":
                    self._send(302, "", {"Location": "http://169.254.169.254/latest/"})
                elif self.path == "/redirect-public":
                    self._send(302, "", {"Location": "http://other.test/"})
                else:
                    self._send(404, "<title>404</title>")

            def do_POST(self) -> None:  # noqa: N802 - stdlib name
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                web.requests.append(("POST", self.headers.get("Host", ""), self.path, body))
                self._send(200, "<title>Danke</title><p>gesendet</p>")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def hosts(self) -> set[str]:
        return {host.split(":")[0] for _m, host, _p, _b in self.requests}

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_web() -> AsyncIterator[FakeWeb]:
    web = FakeWeb()
    yield web  # type: ignore[misc]
    web.stop()


def _dial(web: FakeWeb):  # noqa: ANN202
    async def open_connection(_host: str, _port: int):  # noqa: ANN202
        return await asyncio.open_connection("127.0.0.1", web.port)

    return open_connection


# ---- the proxy's plain-HTTP forwarding ---


async def _raw(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return data


async def test_proxy_forwards_a_plain_http_request_to_a_vetted_host(fake_web: FakeWeb) -> None:
    proxy = EgressProxy(
        allow=("*:*",),
        guard=NetGuard(),
        resolver=fake_resolver,
        open_connection=_dial(fake_web),
        forward_plain_http=True,
    )
    port = await proxy.start()
    try:
        reply = await _raw(
            port,
            b"GET http://public.test/ HTTP/1.1\r\nHost: public.test\r\n"
            b"Proxy-Authorization: Basic c2VjcmV0\r\nProxy-Connection: keep-alive\r\n\r\n",
        )
        assert b"200" in reply.split(b"\r\n", 1)[0] and b"RB 40" in reply
        assert fake_web.requests[-1][:3] == ("GET", "public.test", "/")
    finally:
        await proxy.stop()


@pytest.mark.parametrize(
    "target", [b"http://192.168.0.1/admin", b"http://lan.test/", b"http://169.254.169.254/"]
)
async def test_proxy_refuses_plain_http_to_the_lan(fake_web: FakeWeb, target: bytes) -> None:
    proxy = EgressProxy(
        allow=("*:*",),
        guard=NetGuard(),
        resolver=fake_resolver,
        open_connection=_dial(fake_web),
        forward_plain_http=True,
    )
    port = await proxy.start()
    try:
        reply = await _raw(port, b"GET " + target + b" HTTP/1.1\r\nHost: x\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403")
        assert fake_web.requests == []
    finally:
        await proxy.stop()


async def test_the_sandbox_proxy_still_speaks_only_connect(fake_web: FakeWeb) -> None:
    proxy = EgressProxy(
        allow=("*:*",), guard=NetGuard(), resolver=fake_resolver, open_connection=_dial(fake_web)
    )
    port = await proxy.start()
    try:
        reply = await _raw(port, b"GET http://public.test/ HTTP/1.1\r\nHost: public.test\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 405")
        assert fake_web.requests == []
    finally:
        await proxy.stop()


# ---- real headless Chromium ----------------------------------------------------------------------


@pytest.fixture
async def session(tmp_path: Path, fake_web: FakeWeb) -> AsyncIterator[BrowserSession]:
    if not BrowserSession.availability()[0]:
        pytest.skip("Playwright is not installed")
    settings = BrowserSettings(
        profile_dir=str(tmp_path / "profile"),
        screenshots_dir=str(tmp_path / "shots"),
        navigation_timeout_s=15,
    )
    browser = BrowserSession(
        lambda: settings,
        NetGuard(blocked_hosts=frozenset({"ha.local"})),
        resolver=fake_resolver,
        open_connection=_dial(fake_web),
    )
    try:
        await browser.open("about:blank")
    except BrowserUnavailable as exc:
        pytest.skip(str(exc))
    yield browser
    await browser.close()


pytestmark_browser = pytest.mark.timeout(120)


@pytestmark_browser
async def test_open_and_read_a_public_page(session: BrowserSession, fake_web: FakeWeb) -> None:
    opened = await session.open("http://public.test/")
    assert opened["ok"] is True and opened["title"] == "Fahrplan" and opened["status"] == 200
    read = await session.read(None, 1000)
    assert "12 Min" in read["text"] and read["truncated"] is False
    assert {"text": "Formular", "url": "http://public.test/form"} in read["links"]
    short = await session.read(None, 100)
    assert len(short["text"]) <= 100


@pytestmark_browser
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:{port}/",
        "http://localhost:{port}/",
        "http://lan.test/",
        "http://192.168.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///C:/Windows/win.ini",
    ],
)
async def test_navigation_to_loopback_lan_or_metadata_is_blocked(
    session: BrowserSession, fake_web: FakeWeb, url: str
) -> None:
    result = await session.open(url.format(port=fake_web.port))
    assert result["ok"] is False and result["error"] == "browser.blocked"
    assert fake_web.requests == [], "nothing may reach the network"


@pytestmark_browser
@pytest.mark.parametrize("path", ["/redirect-lan", "/redirect-lan-name", "/redirect-metadata"])
async def test_a_redirect_to_the_lan_is_blocked(
    session: BrowserSession, fake_web: FakeWeb, path: str
) -> None:
    result = await session.open(f"http://public.test{path}")
    assert result["ok"] is False and result["error"] == "browser.blocked"
    assert fake_web.hosts() <= {"public.test"}, fake_web.requests


@pytestmark_browser
async def test_a_redirect_to_another_public_host_is_followed(
    session: BrowserSession, fake_web: FakeWeb
) -> None:
    result = await session.open("http://public.test/redirect-public")
    assert result["ok"] is True and result["url"] == "http://other.test/"


@pytestmark_browser
@pytest.mark.parametrize("path", ["/img-lan", "/script-lan"])
async def test_subresources_and_fetches_to_the_lan_never_leave(
    session: BrowserSession, fake_web: FakeWeb, path: str
) -> None:
    result = await session.open(f"http://public.test{path}")
    assert result["ok"] is True
    await asyncio.sleep(0.5)  # let the page's own requests run
    assert fake_web.hosts() <= {"public.test"}, fake_web.requests


@pytestmark_browser
async def test_the_proxy_alone_still_blocks_a_lan_redirect(
    session: BrowserSession, fake_web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: with the route check switched off, the proxy still refuses the hop."""

    async def allow_everything(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(browser_mod, "judge_request", allow_everything)
    await session.open("http://public.test/redirect-lan-name")
    await session.open("http://public.test/script-lan")
    await asyncio.sleep(0.5)
    assert fake_web.hosts() <= {"public.test"}, fake_web.requests


@pytestmark_browser
async def test_type_and_submit_act_only_on_the_named_origin(
    session: BrowserSession, fake_web: FakeWeb
) -> None:
    assert (await session.open("http://public.test/form"))["ok"]
    wrong = await session.act("type", origin="http://other.test", selector="#q", text="x")
    assert wrong["error"] == "browser.origin_mismatch"
    typed = await session.act("type", origin="http://public.test", selector="#q", text="hallo")
    assert typed["ok"] is True
    sent = await session.act("submit", origin="http://public.test/", selector="#q")
    assert sent["ok"] is True and sent["title"] == "Danke"
    assert ("POST", "public.test", "/send", "q=hallo") in fake_web.requests


@pytestmark_browser
async def test_screenshot_stays_a_local_file(session: BrowserSession, tmp_path: Path) -> None:
    await session.open("http://public.test/")
    shot = await session.screenshot(False)
    assert shot["ok"] is True and shot["stays_local"] is True
    path = Path(shot["path"])
    assert path.parent == tmp_path / "shots"
    assert path.read_bytes().startswith(b"\x89PNG")  # noqa: ASYNC240 - a test reading a file


@pytestmark_browser
async def test_a_form_submit_without_confirmation_sends_nothing(
    session: BrowserSession, fake_web: FakeWeb
) -> None:
    """Through the real executor and permission engine: an unanswered confirmation denies."""
    registry = ToolRegistry()
    register_browser_tools(registry, session)

    class Profiles:
        def get(self, _id: str) -> Profile:
            return Profile(id="companion", rules=[])

    class Privacy:
        def snapshot(self) -> PrivacySnapshot:
            return PrivacySnapshot()

    class Audit:
        def append(self, **_kw: object) -> None:
            pass

    class KillSwitch:
        def is_engaged(self) -> bool:
            return False

    engine = DefaultPermissionEngine(
        profiles=Profiles(),  # type: ignore[arg-type]
        privacy=Privacy(),
        confirm_timeout_s=0.2,
    )
    executor = ToolExecutor(registry, engine, Audit(), None, KillSwitch())  # type: ignore[arg-type]
    assert (
        await executor.call("nox", "browser.open", {"url": "http://public.test/form"}, mode="c")
    ).ok
    before = len(fake_web.requests)
    result = await executor.call(
        "nox",
        "browser.submit",
        {"origin": "http://public.test", "selector": "#q"},
        mode="companion",
    )
    assert result.ok is False and result.decision == Decision.DENY.value
    await asyncio.sleep(0.3)
    assert not [r for r in fake_web.requests[before:] if r[0] == "POST"]
    spec = registry.get("browser.submit")
    assert spec is not None and spec.risk is Risk.MEDIUM


def test_registration() -> None:
    registry = ToolRegistry()
    session = BrowserSession(lambda: BrowserSettings(), NetGuard())
    register_browser_tools(registry, session)
    risks = {name: registry.get(name).risk for name in registry.names()}  # type: ignore[union-attr]
    assert risks == {
        "browser.click": Risk.MEDIUM,
        "browser.open": Risk.LOW,
        "browser.read": Risk.LOW,
        "browser.screenshot": Risk.LOW,
        "browser.submit": Risk.MEDIUM,
        "browser.type": Risk.MEDIUM,
    }
    click = registry.get("browser.click")
    assert click is not None and click.targets is not None
    assert click.targets({"origin": "HTTPS://Shop.Example/x"}) == "origin:https://shop.example"
    assert click.targets({"origin": "javascript:x"}) == "origin:invalid"


async def test_tools_report_unavailable_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ToolRegistry()
    session = BrowserSession(lambda: BrowserSettings(), NetGuard(), resolver=fake_resolver)

    async def no_browser() -> None:
        raise BrowserUnavailable("Chromium could not start; run `playwright install chromium`")

    monkeypatch.setattr(session, "_launch", no_browser)
    register_browser_tools(registry, session)
    spec = registry.get("browser.open")
    assert spec is not None
    result = await spec.handler({"url": "http://public.test/"})
    assert result["error"] == "browser.unavailable" and "playwright install" in result["detail"]
    with contextlib.suppress(Exception):
        await session.close()
