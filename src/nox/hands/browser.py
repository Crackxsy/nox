"""`browser.*`: a headless Chromium Nox drives itself; it reaches public pages and nothing else.

Read tools (`browser.open`, `browser.read`, `browser.screenshot`) are LOW and allowed; the actions
on a page (`browser.click`, `browser.type`, `browser.submit`) are MEDIUM and confirmed, and a
"remember" is scoped to that one action on that one origin (`site:click@https://example.com`).
`browser.*` is a cloud tool to the permission engine, so PRIVATE and OFFLINE refuse all of it.

What makes it safe to point at the open web, below the model:

* **Its own browser.** Playwright's Chromium with a persistent profile under Nox's home
  (`<nox home>/browser/profile`), never the user's browser, cookies or saved passwords. Headless:
  no window ever appears. Downloads are refused, service workers blocked, no permission (camera,
  location, notifications) is ever granted.
* **Every byte through NetGuard.** Chromium is started with Nox's loopback egress proxy as its
  only proxy, with the implicit loopback bypass removed (`<-loopback>`). The proxy resolves each
  host itself, judges the *resolved* address with the same `NetGuard` rules as `web.fetch` (no
  loopback, LAN, link-local, cloud metadata or Home Assistant) and dials the address it judged.
  That covers top-level navigations, redirects (each hop is a new request), form submissions,
  subresources, fetch/XHR and WebSockets alike - and DNS rebinding, because Chromium never
  resolves a proxied name itself.
* **A second, earlier check** in a route handler: a navigation whose host resolves anywhere
  private is aborted before it reaches the network, a subresource to a literal private address or
  `localhost` likewise, and every scheme other than http(s)/data/blob/about is refused
  (`file:`, `chrome:` ...). Playwright does not route the hops of a server redirect, so those are
  the proxy's alone - and after every navigation the page's final URL is judged once more: a page
  that ended up on a forbidden host (the proxy's refusal page) is reported as blocked and left.
* **WebRTC** is the one way a page can open sockets past a proxy (UDP); it is switched off by
  Chromium flags and the page's `RTCPeerConnection` is removed before any script runs.
* Page actions are Playwright calls *inside* the headless page (DevTools protocol). Nothing here
  synthesises OS-level keyboard or mouse input; the user's desktop and any running game are
  untouched.
* An action names the `origin` it means to act on, and is refused when the page is not on that
  origin any more - a confirmation for `https://shop.example` never clicks on a page a script has
  since navigated elsewhere.
* Page text comes back truncated, and as a tool result it reaches the model as untrusted data
  (`[[DATA source=tool:browser.read]]`) that taints the turn: after it, a remembered grant above
  LOW asks again.

Limits, stated honestly: a page can still make Chromium contact *public* hosts of its choosing
(that is what browsing is); a proxied plain-HTTP request may reuse a vetted connection for another
host name on the same public address; and when Playwright or its Chromium is missing - a Pi
without `playwright install chromium` - the tools say so instead of pretending.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.sandbox.netguard import BlockedAddress, NetGuard, resolve_all
from nox.sandbox.proxy import EgressProxy, Resolver, StreamPair
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = [
    "BrowserSession",
    "BrowserSettings",
    "judge_request",
    "page_origin",
    "register_browser_tools",
]

#: Chromium switches: no WebRTC sockets outside the proxy, no background networking of its own.
CHROMIUM_ARGS: tuple[str, ...] = (
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=DnsOverHttps,MediaRouter,OptimizationHints,Translate",
)
#: Runs before any page script: no peer connections at all.
_NO_WEBRTC = """
for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel']) {
  try {
    Object.defineProperty(window, name, { value: undefined, configurable: false });
  } catch (e) {}
}
"""
_PASSIVE_SCHEMES = frozenset({"data", "blob", "about"})
#: One page action, done in a single in-page evaluation that first asserts the page's origin.
#: JavaScript in a page runs to completion, so nothing can navigate between the check and the
#: act: either the page is still on the confirmed origin and the action happens there, or it
#: throws and nothing happens. (A Playwright `click()`/`fill()` after a separate origin check
#: could land on a page a script had navigated to in between.)
_ACT_JS = """
(el, [action, origin, text]) => {
  if (window.location.origin !== origin) {
    throw new Error('nox-origin-mismatch:' + window.location.origin);
  }
  if (action === 'click') {
    el.click();
  } else if (action === 'type') {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLInputElement ? HTMLInputElement.prototype : null;
    el.focus();
    if (proto) {
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
    } else if (el.isContentEditable) {
      el.textContent = text;
    } else {
      throw new Error('not a text field');
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  } else if (action === 'submit') {
    const form = el.tagName === 'FORM' ? el : (el.form || el.closest('form'));
    if (!form) { throw new Error('no form for this element'); }
    form.requestSubmit();
  } else {
    throw new Error('unknown action');
  }
}
"""


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    enabled: bool = True
    profile_dir: str = ""
    screenshots_dir: str = ""
    max_text_chars: int = 20_000
    navigation_timeout_s: float = 20.0
    keep_screenshots: int = 50


def page_origin(url: str) -> str:
    """`scheme://host[:port]` for an http(s) URL (default port dropped), else ""."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    host = parts.hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" if port in (None, default) else f"{scheme}://{host}:{port}"


def _literal_reason(host: str, guard: NetGuard) -> str | None:
    """A reason to refuse `host` without resolving it: a private literal IP, or a local name."""
    name = host.strip("[]").lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return "loopback name"
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return None
    try:
        guard.check_addresses(name, [ip])
    except BlockedAddress as blocked:
        return blocked.reason
    return None


async def judge_request(
    url: str, *, navigation: bool, guard: NetGuard, resolver: Resolver = resolve_all
) -> str | None:
    """Why the browser must not make this request, or None. The route handler's check.

    A navigation (top-level or frame, including each redirect hop the page itself follows) is
    resolved and judged by address; a subresource is judged on its literal host here and by
    address in the proxy, which sees every connection anyway.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in _PASSIVE_SCHEMES:
        return None
    if scheme not in ("http", "https"):
        return f"scheme {scheme!r} is not allowed"
    host = parts.hostname or ""
    if not host:
        return "no host"
    if host.lower().rstrip(".") in guard.blocked_hosts:
        return "explicitly blocked host"
    literal = _literal_reason(host, guard)
    if literal is not None:
        return literal
    if not navigation:
        return None
    try:
        port = parts.port or (443 if scheme == "https" else 80)
        addresses = await asyncio.to_thread(resolver, host, port)
        guard.check_addresses(host, addresses)
    except BlockedAddress as blocked:
        return blocked.reason
    except (OSError, ValueError) as exc:
        return f"cannot resolve: {type(exc).__name__}"
    return None


Launcher = Callable[["BrowserSession", int], Awaitable[Any]]


class BrowserSession:
    """One headless Chromium, started on first use, with one page Nox works in."""

    def __init__(
        self,
        settings: Callable[[], BrowserSettings],
        guard: NetGuard,
        *,
        resolver: Resolver = resolve_all,
        open_connection: Callable[[str, int], Awaitable[StreamPair]] | None = None,
    ) -> None:
        self._settings = settings
        self._guard = guard
        self._resolver = resolver
        self._open_connection = open_connection
        self._lock = asyncio.Lock()
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._proxy: EgressProxy | None = None
        #: Requests the guards refused since the last navigation began (url, reason).
        self.blocked: list[tuple[str, str]] = []

    # -- availability -------------------------------------------------------------------------

    @staticmethod
    def availability() -> tuple[bool, str]:
        """Whether Playwright and its Chromium are installed here, without starting anything."""
        import importlib.util

        if importlib.util.find_spec("playwright") is None:
            return False, "Playwright is not installed (pip extra `browser`)"
        return True, ""

    @property
    def current_url(self) -> str:
        return str(self._page.url) if self._page is not None else ""

    # -- lifecycle ------------------------------------------------------------------------------

    async def _ensure(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._context is None:
            await self._launch()
        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[0] if pages else await self._context.new_page()
        return self._page

    async def _launch(self) -> None:
        cfg = self._settings()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable("Playwright is not installed (pip extra `browser`)") from exc
        profile = Path(cfg.profile_dir)
        await asyncio.to_thread(profile.mkdir, parents=True, exist_ok=True)
        proxy = EgressProxy(
            allow=("*:*",),
            guard=self._guard,
            resolver=self._resolver,
            open_connection=self._open_connection,
            forward_plain_http=True,
            # A loopback proxy is reachable by every local program; without its secret, which
            # only this browser gets, it answers 407 and serves no one else.
            credentials=EgressProxy.new_credentials(),
        )
        port = await proxy.start()
        user, password = proxy.credentials or ("", "")
        pw = await async_playwright().start()
        try:
            context = await pw.chromium.launch_persistent_context(
                str(profile),
                headless=True,  # never a visible window, not even as an option
                proxy={
                    "server": f"http://127.0.0.1:{port}",
                    "bypass": "<-loopback>",
                    "username": user,
                    "password": password,
                },
                accept_downloads=False,
                service_workers="block",
                permissions=[],
                args=list(CHROMIUM_ARGS),
            )
            await context.route("**/*", self._on_route)
            await context.add_init_script(_NO_WEBRTC)
            context.set_default_timeout(cfg.navigation_timeout_s * 1000)
        except Exception as exc:
            await pw.stop()
            await proxy.stop()
            raise BrowserUnavailable(
                f"Chromium could not start ({type(exc).__name__}); run `playwright install "
                "chromium`"
            ) from exc
        self._pw, self._context, self._proxy = pw, context, proxy
        log.info("browser.started", proxy_port=port)

    async def close(self) -> None:
        context, pw, proxy = self._context, self._pw, self._proxy
        self._context = self._pw = self._page = self._proxy = None
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        if pw is not None:
            with contextlib.suppress(Exception):
                await pw.stop()
        if proxy is not None:
            await proxy.stop()

    async def _on_route(self, route: Any, request: Any) -> None:
        url = str(request.url)
        reason = await judge_request(
            url,
            navigation=bool(request.is_navigation_request()),
            guard=self._guard,
            resolver=self._resolver,
        )
        if reason is None:
            await route.continue_()
            return
        self.blocked.append((url, reason))
        log.warning("browser.request_blocked", host=urlsplit(url).hostname or "", reason=reason)
        await route.abort("blockedbyclient")

    # -- tools ----------------------------------------------------------------------------------

    async def open(self, url: str) -> dict[str, Any]:
        reason = await judge_request(
            url, navigation=True, guard=self._guard, resolver=self._resolver
        )
        if reason is not None:
            return {"ok": False, "error": "browser.blocked", "detail": reason}
        async with self._lock:
            page = await self._ensure()
            self.blocked.clear()
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001 - Playwright raises its own Error types
                return self._navigation_error(exc)
            landed = await self._judge_landing(page)
            if landed is not None:
                return landed
            return {
                "ok": True,
                "url": page.url,
                "status": response.status if response is not None else None,
                "title": await page.title(),
            }

    async def read(self, selector: str | None, max_chars: int) -> dict[str, Any]:
        async with self._lock:
            if self._page is None:
                return {"ok": False, "error": "browser.no_page", "detail": "open a page first"}
            page = self._page
            limit = min(max_chars, self._settings().max_text_chars)
            try:
                text = await page.locator(selector or "body").first.inner_text()
                links = await page.eval_on_selector_all(
                    "a[href]",
                    "els => els.slice(0, 40).map(a => [a.innerText.trim().slice(0, 80), a.href])",
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": "browser.read_failed", "detail": type(exc).__name__}
            return {
                "ok": True,
                "url": page.url,
                "title": await page.title(),
                "text": text[:limit],
                "truncated": len(text) > limit,
                "links": [{"text": t, "url": h} for t, h in links if page_origin(h)],
            }

    async def screenshot(self, full_page: bool) -> dict[str, Any]:
        cfg = self._settings()
        async with self._lock:
            if self._page is None:
                return {"ok": False, "error": "browser.no_page", "detail": "open a page first"}
            folder = Path(cfg.screenshots_dir)
            await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
            path = folder / f"browser-{int(time.time())}-{uuid.uuid4().hex[:8]}.png"
            await self._page.screenshot(path=str(path), full_page=full_page)
            await asyncio.to_thread(_prune, folder, "browser-*.png", cfg.keep_screenshots)
            return {"ok": True, "path": str(path), "url": self._page.url, "stays_local": True}

    async def act(
        self, action: str, *, origin: str, selector: str, text: str = ""
    ) -> dict[str, Any]:
        """click / type / submit on the current page, only while it is on `origin`."""
        async with self._lock:
            if self._page is None:
                return {"ok": False, "error": "browser.no_page", "detail": "open a page first"}
            page = self._page
            expected = page_origin(origin)
            actual = page_origin(page.url)
            if not expected or expected != actual:
                return {
                    "ok": False,
                    "error": "browser.origin_mismatch",
                    "detail": f"the page is on {actual or 'no web origin'}, not {expected}",
                }
            self.blocked.clear()
            target = page.locator(selector).first
            # The check above is for a clear message; the authority is the in-page assertion in
            # `_ACT_JS`, atomic with the action. A submit navigates; a click may: wait for it.
            wait_ms = {"submit": 15_000, "click": 2_000}.get(action, 0)
            acted = False
            try:
                async with contextlib.AsyncExitStack() as stack:
                    if wait_ms:
                        await stack.enter_async_context(
                            page.expect_navigation(wait_until="domcontentloaded", timeout=wait_ms)
                        )
                    await target.evaluate(_ACT_JS, [action, expected, text])
                    acted = True
            except Exception as exc:  # noqa: BLE001 - Playwright raises its own Error types
                if not acted:
                    if "nox-origin-mismatch:" in str(exc):
                        moved = str(exc).split("nox-origin-mismatch:", 1)[1].split()[0]
                        return {
                            "ok": False,
                            "error": "browser.origin_mismatch",
                            "detail": f"the page moved to {moved} before the action; nothing "
                            "was done",
                        }
                    return self._navigation_error(exc)
                # The action ran and no navigation followed within the window: that is fine.
            landed = await self._judge_landing(page)
            if landed is not None:
                return landed
            if self.blocked:
                return self._blocked_result()
            return {"ok": True, "url": page.url, "title": await page.title()}

    async def _judge_landing(self, page: Any) -> dict[str, Any] | None:
        """Where a navigation ended, judged again: a redirect hop is the proxy's to refuse, and
        Chromium then shows the proxy's refusal *as* the page. Report that as blocked and leave
        it, so nothing from a forbidden host is ever read or acted on."""
        reason = await judge_request(
            page.url, navigation=True, guard=self._guard, resolver=self._resolver
        )
        if reason is None:
            return None
        self.blocked.append((page.url, reason))
        log.warning("browser.landing_blocked", host=urlsplit(page.url).hostname or "")
        with contextlib.suppress(Exception):
            await page.goto("about:blank")
        return self._blocked_result()

    def _blocked_result(self) -> dict[str, Any]:
        url, reason = self.blocked[-1]
        return {
            "ok": False,
            "error": "browser.blocked",
            "detail": f"{urlsplit(url).hostname or url}: {reason}",
        }

    def _navigation_error(self, exc: Exception) -> dict[str, Any]:
        if self.blocked:
            return self._blocked_result()
        message = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        if "ERR_TUNNEL_CONNECTION_FAILED" in message or "ERR_PROXY" in message:
            return {
                "ok": False,
                "error": "browser.blocked",
                "detail": "refused by the egress proxy",
            }
        return {"ok": False, "error": "browser.failed", "detail": message}


class BrowserUnavailable(RuntimeError):  # noqa: N818 - a state, like SandboxUnavailable
    """Playwright or its Chromium is not installed (or cannot start) on this machine."""


def _prune(folder: Path, pattern: str, keep: int) -> None:
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[max(keep, 1) :]:
        with contextlib.suppress(OSError):
            old.unlink()


# ---- registration --------------------------------------------------------------------------------


class OpenInput(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class ReadInput(BaseModel):
    selector: str | None = Field(default=None, max_length=512)
    max_chars: int = Field(default=8000, ge=100, le=50_000)


class ScreenshotInput(BaseModel):
    full_page: bool = False


class ClickInput(BaseModel):
    #: The origin (`https://shop.example`) the action is meant for; refused if the page left it.
    origin: str = Field(min_length=1, max_length=512)
    selector: str = Field(min_length=1, max_length=512)


class TypeInput(ClickInput):
    text: str = Field(max_length=10_000)


def _origin_target(payload: dict[str, Any]) -> str:
    origin = page_origin(str(payload.get("origin", "")))
    return f"origin:{origin or 'invalid'}"


def register_browser_tools(registry: ToolRegistry, session: BrowserSession) -> None:
    """Register the six `browser.*` tools onto `registry`, all served by `session`."""

    def unavailable(exc: BrowserUnavailable) -> dict[str, Any]:
        return {"ok": False, "error": "browser.unavailable", "detail": str(exc)}

    async def guarded(call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            return await call()
        except BrowserUnavailable as exc:
            return unavailable(exc)

    async def open_(payload: dict[str, Any]) -> dict[str, Any]:
        model = OpenInput.model_validate(payload)
        return await guarded(lambda: session.open(model.url))

    async def read(payload: dict[str, Any]) -> dict[str, Any]:
        model = ReadInput.model_validate(payload)
        return await guarded(lambda: session.read(model.selector, model.max_chars))

    async def screenshot(payload: dict[str, Any]) -> dict[str, Any]:
        model = ScreenshotInput.model_validate(payload)
        return await guarded(lambda: session.screenshot(model.full_page))

    def actor(action: str) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        async def handler(payload: dict[str, Any]) -> dict[str, Any]:
            model = TypeInput.model_validate({"text": "", **payload})
            return await guarded(
                lambda: session.act(
                    action, origin=model.origin, selector=model.selector, text=model.text
                )
            )

        return handler

    reads: list[tuple[str, str, type[BaseModel], Any, Callable[[dict[str, Any]], str]]] = [
        (
            "browser.open",
            "Open a public http(s) page in Nox's own headless browser. Never reaches the LAN, "
            "loopback or a metadata address; redirects are checked too.",
            OpenInput,
            open_,
            lambda p: str(p.get("url", "")),
        ),
        (
            "browser.read",
            "Read the visible text (and links) of the page open in Nox's browser, truncated.",
            ReadInput,
            read,
            lambda _p: session.current_url,
        ),
        (
            "browser.screenshot",
            "Save a screenshot of the page open in Nox's browser to a local file (it stays "
            "local; describe it with vision.describe if needed).",
            ScreenshotInput,
            screenshot,
            lambda _p: session.current_url,
        ),
    ]
    for name, description, model, handler, targets in reads:
        registry.register(
            ToolSpec(
                name=name,
                description=description,
                input_model=model,
                risk=Risk.LOW,
                handler=handler,
                side_effects=False,
                local=False,
                targets=targets,
            )
        )
    actions: list[tuple[str, type[BaseModel], str]] = [
        ("click", ClickInput, "Click an element (CSS selector) on the page open in Nox's browser."),
        ("type", TypeInput, "Fill text into a field (CSS selector) on the page in Nox's browser."),
        (
            "submit",
            ClickInput,
            "Submit the form that contains the element (CSS selector) in Nox's browser.",
        ),
    ]
    for action, action_model, description in actions:
        registry.register(
            ToolSpec(
                name=f"browser.{action}",
                description=description + " Needs the page's origin; asks first.",
                input_model=action_model,
                risk=Risk.MEDIUM,
                handler=actor(action),
                side_effects=True,
                local=False,
                rememberable=True,
                targets=_origin_target,
            )
        )
