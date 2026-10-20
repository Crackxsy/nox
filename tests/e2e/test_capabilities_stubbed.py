"""Click test of the dashboard's "Fähigkeiten" page against a stubbed IPC, recorded on video.

Unlike `test_dashboard_live.py` this boots no core. The built dashboard bundle is served from
`ui/dashboard/dist` through `page.route`, and the IPC websocket is answered by a small in-test
stub through `page.route_web_socket` - no port is opened, nothing leaves the browser. The stub
speaks Envelope v1 exactly as far as the page needs: `ipc.auth`, `ipc.subscribe`, and the requests
of the page, with a little state so the clicks have visible consequences.

The flow, at phone, tablet and desktop size, headless, with a video per size:
add an MCP server with a secret header (the secret goes to `secrets.set`, `mcp.servers.add` only
gets the `secret:` reference), allow its first start, then activate a self-taught skill, which the
stub answers with `needs_pin` first so the PIN field has to appear before the second try.

Videos land in pytest-playwright's output directory (`test-results/` by default).
"""

from __future__ import annotations

import json
import mimetypes
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, Route, ViewportSize, WebSocketRoute, expect

from nox.app import DASHBOARD_DIST

pytestmark = pytest.mark.e2e

ORIGIN = "http://nox.test"
WS_PORT = 47999
WS_URL = f"ws://nox.test:{WS_PORT}/ws"
SECRET_VALUE = "Bearer e2e-very-secret"
PIN = "2468"

SIZES: dict[str, ViewportSize] = {
    "phone": {"width": 390, "height": 844},
    "tablet": {"width": 820, "height": 1180},
    "desktop": {"width": 1440, "height": 900},
}


@dataclass
class StubCore:
    """The core as far as the page can tell: request name -> answer, with a little state."""

    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    servers: list[dict[str, Any]] = field(default_factory=list)
    skill_state: str = "ready"

    def skill(self) -> dict[str, Any]:
        return {
            "id": "bahn",
            "intent": "Abfahrten am Bahnhof Springfield ansagen",
            "state": self.skill_state,
            "created_at": "2026-10-19T08:00:00+00:00",
            "updated_at": "2026-10-19T08:05:00+00:00",
            "version": 1,
            "rounds": 2,
            "problems": [],
            "message": "",
            "approved_hosts": [],
            "summary": {
                "tools": [{"name": "bahn.departures", "risk": "low", "description": "Abfahrten"}],
                "hosts": ["v6.db.transport.rest"],
                "new_hosts": ["v6.db.transport.rest"],
                "secrets": [],
                "secrets_present": {},
                "tests_passed": 4,
                "tests_failed": 0,
                "test_output": "4 passed",
                "needs_pin": True,
            },
        }

    def answer(self, name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The response payload, or None for "unknown request" (answered as an error)."""
        self.requests.append((name, payload))
        if name == "ipc.subscribe":
            return {"patterns": payload.get("patterns", [])}
        if name == "security.pin.status":
            return {"configured": True}
        if name == "secrets.set":
            return {"ok": True}
        if name == "mcp.servers.list":
            return {"sandboxed": False, "servers": self.servers}
        if name == "mcp.servers.add":
            self.servers.append(
                {
                    "id": payload["id"],
                    "name": payload["name"] or payload["id"],
                    "transport": "http",
                    "command": [],
                    "url": payload["url"],
                    "version": payload["version"],
                    "enabled": True,
                    "network": True,
                    "state": "needs_approval",
                    "reason": "",
                    "sandboxed": False,
                    "secrets": [v[len("secret:") :] for v in payload["headers"].values()],
                    "tools": [],
                }
            )
            return {"ok": True, "state": "needs_approval"}
        if name == "mcp.servers.approve":
            for server in self.servers:
                if server["id"] == payload["id"]:
                    server["state"] = "running"
                    server["tools"] = [
                        {
                            "name": f"mcp.{server['id']}.forecast",
                            "remote_name": "forecast",
                            "description": "<img src=x onerror=alert(1)> Vorhersage",
                            "read_only": False,
                            "server_says_read_only": True,
                            "blocked": "",
                        }
                    ]
            return {"ok": True, "state": "running", "reason": ""}
        if name == "skills.list":
            return {"available": True, "reason": "", "skills": [self.skill()]}
        if name == "skills.activate":
            if payload.get("pin") != PIN:
                return {"ok": False, "needs_pin": True, "reason": "PIN required"}
            self.skill_state = "active"
            return {"ok": True, "state": "running"}
        if name == "watch.list":
            return {"watchers": []}
        return None


def _envelope(kind: str, name: str, payload: dict[str, Any], corr: str | None) -> str:
    return json.dumps(
        {
            "v": 1,
            "id": str(uuid.uuid4()),
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "name": name,
            "corr": corr,
            "src": {"role": "core", "id": "core"},
            "payload": payload,
        }
    )


def _install_stub(page: Page, stub: StubCore) -> None:
    dist = DASHBOARD_DIST.resolve()

    def serve(route: Route) -> None:
        path = route.request.url.split("?", 1)[0].split("#", 1)[0]
        rel = path[len(f"{ORIGIN}/dashboard/") :] or "index.html"
        file = (dist / rel).resolve()
        if not file.is_relative_to(dist) or not file.is_file():
            route.fulfill(status=404, body="not found")
            return
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        route.fulfill(status=200, body=file.read_bytes(), content_type=mime)

    def on_socket(ws: WebSocketRoute) -> None:
        def on_message(message: str | bytes) -> None:
            env = json.loads(message)
            name, corr, payload = env["name"], env["id"], env.get("payload", {})
            if name == "ipc.auth":
                ws.send(_envelope("response", name, {"ok": True, "session_id": "e2e"}, corr))
                return
            answer = stub.answer(name, payload)
            if answer is None:
                error = {"code": "not_found", "message": f"unknown request {name}"}
                ws.send(_envelope("error", name, error, corr))
            else:
                ws.send(_envelope("response", name, answer, corr))

        ws.on_message(on_message)

    page.route(f"{ORIGIN}/dashboard/**", serve)
    page.route_web_socket(WS_URL, on_socket)


@pytest.fixture(scope="module")
def dashboard_built() -> None:
    if not (DASHBOARD_DIST / "index.html").is_file():
        pytest.skip("ui/dashboard/dist is not built (npm run build in ui/dashboard)")


@pytest.fixture
def recorded(
    browser: Browser, output_path: str, request: pytest.FixtureRequest
) -> Iterator[tuple[Page, StubCore]]:
    size = request.param
    video_dir = Path(output_path) / size
    context = browser.new_context(
        viewport=SIZES[size],
        record_video_dir=str(video_dir),
        record_video_size=SIZES[size],
        locale="de-DE",
        reduced_motion="reduce",
    )
    page = context.new_page()
    stub = StubCore()
    _install_stub(page, stub)
    try:
        yield page, stub
    finally:
        context.close()  # finishes the video file


def _no_horizontal_scroll(page: Page) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"page scrolls sideways by {overflow}px"


@pytest.mark.usefixtures("dashboard_built")
@pytest.mark.parametrize("recorded", list(SIZES), indirect=True)
def test_add_mcp_server_and_activate_skill(recorded: tuple[Page, StubCore]) -> None:
    page, stub = recorded
    page.goto(f"{ORIGIN}/dashboard/?ws={WS_PORT}&lang=de#token=e2e-token")
    expect(page.locator(".app[data-conn=online]")).to_have_count(1, timeout=15000)

    # Geräte & Fähigkeiten › Fähigkeiten; extensions sit under "Für Fortgeschrittene".
    page.get_by_role("navigation").get_by_role("link", name=re.compile("^Geräte")).first.click()
    tab = page.get_by_role("tab", name="Fähigkeiten")
    tab.click()
    panel = page.get_by_role("tabpanel", name="Fähigkeiten")
    expect(panel.get_by_role("heading", name="Erweiterungen", exact=True)).to_be_visible()
    expect(panel.get_by_text("Nicht isoliert:")).to_be_visible()
    _no_horizontal_scroll(page)

    # -- add a remote MCP server with a secret header ---------------------------------------------
    panel.get_by_role("button", name="Fähigkeit hinzufügen").click()
    form = panel.locator("form")
    form.get_by_label("Adresse im Netz").check()
    form.get_by_label("Name", exact=True).fill("Wetterdienst")
    form.get_by_label("Kurzname").fill("wetter")
    form.get_by_label("Adresse (https)").fill("https://wetter.example/mcp")
    form.get_by_label("Version").fill("2.0.0")
    form.get_by_role("button", name="Zeile hinzufügen").click()
    form.get_by_label("Name (Zeile 1)").fill("Authorization")
    form.get_by_label("Wert (Zeile 1)").fill(SECRET_VALUE)
    form.get_by_label("geheim (Zeile 1)").check()
    form.get_by_label("PIN").fill(PIN)
    _no_horizontal_scroll(page)
    form.get_by_role("button", name="Hinzufügen", exact=True).click()

    card = panel.get_by_role("region", name="Wetterdienst")
    expect(card.get_by_text("wartet auf Erlaubnis")).to_be_visible()
    expect(card.locator("pre")).to_contain_text("https://wetter.example/mcp")
    expect(card.locator("pre")).to_contain_text("2.0.0")

    secret_calls = [p for n, p in stub.requests if n == "secrets.set"]
    assert secret_calls == [
        {"name": "nox/mcp/wetter/authorization", "value": SECRET_VALUE, "pin": PIN}
    ]
    add = next(p for n, p in stub.requests if n == "mcp.servers.add")
    assert add["headers"] == {"Authorization": "secret:nox/mcp/wetter/authorization"}
    assert SECRET_VALUE not in json.dumps(add)

    # -- allow the first start --------------------------------------------------------------------
    card.get_by_role("button", name="Ersten Start erlauben").click()
    expect(card.get_by_text("läuft", exact=True)).to_be_visible()
    card.get_by_text("Werkzeuge (1)").click()
    # The server's description stays text; no element is created from it.
    expect(card.get_by_text("<img src=x onerror=alert(1)> Vorhersage")).to_be_visible()
    assert card.locator("img").count() == 0
    _no_horizontal_scroll(page)

    # -- activate the self-taught skill: the PIN is asked for only after needs_pin -----------------
    skill = panel.get_by_role("region", name="Abfahrten am Bahnhof Springfield ansagen")
    skill.scroll_into_view_if_needed()
    expect(skill.get_by_text("neu – wird mit dem Aktivieren erlaubt")).to_be_visible()
    expect(skill.get_by_label("PIN")).to_have_count(0)
    skill.get_by_role("button", name="Aktivieren").click()
    expect(skill.get_by_label("PIN")).to_be_visible()
    skill.get_by_label("PIN").fill(PIN)
    skill.get_by_role("button", name="Aktivieren").click()
    expect(skill.get_by_text("aktiv", exact=True)).to_be_visible()
    expect(skill.get_by_role("button", name="Deaktivieren")).to_be_visible()

    activations = [p for n, p in stub.requests if n == "skills.activate"]
    assert activations == [{"id": "bahn"}, {"id": "bahn", "pin": PIN}]
    _no_horizontal_scroll(page)
