"""Click tests of the redesigned dashboard (Spec UX-Neubau) against a stubbed IPC, on video.

Like `test_capabilities_stubbed.py` this boots no core: the built bundle is served from
`ui/dashboard/dist` through `page.route`, and the IPC websocket is answered by an in-test stub
through `page.route_web_socket` - no port is opened, nothing leaves the browser. The stub keeps a
little state and pushes the events a core would (`settings.changed`, `security.kill_switch`,
`remote.paired`, ...), so every click has a visible consequence.

One journey per size (phone 390x844, tablet 834x1194, desktop 1440x900) and colour scheme:
first run -> ask Nox something -> allow a permission question on Heute and deny one in the sheet ->
pair a phone -> add an extension (MCP) -> change the Modus -> set up the stream -> Stopp and
Fortsetzen. After the main steps the page is checked with axe-core (WCAG 2.2 AA rules, zero
findings) and for sideways scrolling.

Artifacts (one video per journey, a screenshot per step) go to `NOX_UX_ARTIFACTS` when set,
otherwise to pytest-playwright's output directory.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import (
    Browser,
    Locator,
    Page,
    Route,
    ViewportSize,
    WebSocketRoute,
    expect,
)

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "ui" / "dashboard" / "dist"
AXE = ROOT / "ui" / "dashboard" / "node_modules" / "axe-core" / "axe.min.js"
ORIGIN = "http://nox.test"
WS_PORT = 47998
WS_URL = f"ws://nox.test:{WS_PORT}/ws"
UI_TIMEOUT_MS = 15000

SIZES: dict[str, ViewportSize] = {
    "phone": {"width": 390, "height": 844},
    "tablet": {"width": 834, "height": 1194},
    "desktop": {"width": 1440, "height": 900},
}
SCHEMES = ("light", "dark")
QR_SVG = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 21 21'>"
    "<rect width='21' height='21' fill='white'/>"
    "<rect x='1' y='1' width='7' height='7'/><rect x='13' y='1' width='7' height='7'/>"
    "<rect x='1' y='13' width='7' height='7'/><rect x='10' y='10' width='3' height='3'/></svg>"
)


def _now(offset_s: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat()


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


@dataclass
class StubCore:
    """The core as far as the page can tell: request name -> answer, plus pushed events."""

    ws: WebSocketRoute | None = None
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    values: dict[str, Any] = field(
        default_factory=lambda: {
            "identity.name": "Nox",
            "identity.user_display_name": "",
            "voice.tts.engine": "piper",
        }
    )
    mode: str = "companion"
    privacy: str = "balanced"
    level: str = "running"
    devices: list[dict[str, Any]] = field(default_factory=list)
    servers: list[dict[str, Any]] = field(default_factory=list)
    overlay: bool = False

    def push(self, name: str, payload: dict[str, Any]) -> None:
        assert self.ws is not None, "the page has not connected"
        self.ws.send(_envelope("event", name, payload, None))

    def ask_permission(
        self, request_id: str, action: str, target: str, risk: str = "medium"
    ) -> None:
        self.push(
            "security.permission_requested",
            {
                "request_id": request_id,
                "agent": "companion",
                "tool": "desktop",
                "action": action,
                "mode": self.mode,
                "risk": risk,
                "target": target,
                "expires_at": _now(55),
                "remember_ttl_s": 3600,
            },
        )

    def names(self) -> list[str]:
        return [name for name, _ in self.requests]

    def schema(self) -> list[dict[str, Any]]:
        return [
            {"path": "identity.name", "type": "string", "group": "identity"},
            {"path": "identity.user_display_name", "type": "string", "group": "identity"},
            {
                "path": "voice.tts.engine",
                "type": "enum",
                "options": ["piper", "kokoro", "supertonic"],
                "group": "voice",
            },
            {"path": "desktop.computer_use.enabled", "type": "bool", "group": "privacy"},
            {
                "path": "stream.twitch.channel",
                "type": "string",
                "group": "integrations",
                "restart_required": True,
            },
        ]

    def answer(self, name: str, p: dict[str, Any]) -> dict[str, Any] | None:  # noqa: C901, PLR0911, PLR0912
        """The response payload, or None for "unknown request" (answered as an error)."""
        self.requests.append((name, p))
        if name == "ipc.subscribe":
            return {"patterns": p.get("patterns", [])}
        if name == "state.get":
            return {
                "assistant": {"mode": self.mode, "muted": False},
                "privacy": {
                    "mode": self.privacy,
                    "microphone": True,
                    "camera": False,
                    "screen": False,
                },
                "voice": {
                    "stt_engine": "whisper",
                    "tts_engine": "piper",
                    "listening": True,
                    "speaking": False,
                    "routing": "private",
                },
                "system": {"level": self.level},
            }
        if name == "health.get":
            return {
                "generated_at": _now(),
                "components": {
                    "ai.rules": {"status": "available", "reason": ""},
                    "voice": {"status": "available", "reason": ""},
                    "vault": {"status": "available", "reason": ""},
                },
            }
        if name == "ai.providers":
            return {
                "providers": [
                    {
                        "id": "ai.rules",
                        "display_name": "Regeln",
                        "local": True,
                        "roles": ["chat"],
                        "status": "available",
                    }
                ]
            }
        if name == "health.history":
            return {"entries": []}
        if name == "security.permission.pending":
            return {"pending": []}
        if name == "security.permission.reply":
            return {"ok": True}
        if name == "security.pin.status":
            return {"configured": False}
        if name == "lan.status":
            return {
                "enabled": True,
                "running": True,
                "urls": ["https://192.168.1.20:47843/dashboard/"],
                "port": 47843,
                "fingerprint": "AB:CD:EF:01",
                "ca_fingerprint": "12:34:56:78",
                "pin_set": False,
            }
        if name == "lan.devices.list":
            return {"devices": self.devices}
        if name == "lan.pair.start":
            return {
                "code": "K7F29Q",
                "expires_at": _now(300),
                "url": "https://192.168.1.20:47843/dashboard/#pair=K7F29Q",
                "urls": ["https://192.168.1.20:47843/dashboard/"],
                "qr": QR_SVG,
                "fingerprint": "AB:CD:EF:01",
            }
        if name == "remote.devices.list":
            return {"devices": []}
        if name == "memory.profile.list":
            return {
                "items": [
                    {
                        "id": 1,
                        "type": "user",
                        "text": "Jonas hat am 2. November Geburtstag.",
                        "created_at": _now(-3600),
                        "learned": True,
                        "unconfirmed": True,
                    }
                ]
            }
        if name in ("memory.profile.confirm", "memory.profile.forget"):
            return {"ok": True}
        if name == "presets.list":
            return {
                "enabled": True,
                "actions": [],
                "presets": [
                    {
                        "id": "stream_prep",
                        "name": "Stream vorbereiten",
                        "enabled": True,
                        "triggers": {
                            "phrases": ["lass uns streamen"],
                            "at": "",
                            "days": [],
                            "on_process_start": "",
                            "on_process_end": "",
                        },
                        "steps": [],
                    }
                ],
            }
        if name == "presets.activate":
            return {
                "preset_id": p.get("preset"),
                "name": "Stream vorbereiten",
                "trigger": "ui",
                "ok": True,
                "steps": [],
                "duration_ms": 12,
            }
        if name == "config.get":
            return {"values": self.values, "schema": self.schema(), "user_config_path": "user.yaml"}
        if name == "config.set":
            self.values.update(p.get("values", {}))
            self.push("settings.changed", {"paths": list(p.get("values", {}))})
            return {
                "ok": True,
                "applied": list(p.get("values", {})),
                "restart_required": [],
                "errors": {},
            }
        if name == "secrets.status":
            return {"secrets": []}
        if name == "personality.get":
            return {"text": "Du bist Nox.", "path": "personality.md"}
        if name == "twitch.auth.status":
            return {"state": "idle"}
        if name == "skills.list":
            return {"available": True, "reason": "", "skills": []}
        if name == "watch.list":
            return {"watchers": []}
        if name == "mcp.servers.list":
            return {"sandboxed": True, "servers": self.servers}
        if name == "mcp.servers.add":
            self.servers.append(
                {
                    "id": p["id"],
                    "name": p.get("name") or p["id"],
                    "transport": "http",
                    "command": [],
                    "url": p.get("url", ""),
                    "version": p.get("version", ""),
                    "enabled": True,
                    "network": True,
                    "state": "needs_approval",
                    "reason": "",
                    "sandboxed": True,
                    "secrets": [],
                    "tools": [],
                }
            )
            return {"ok": True, "state": "needs_approval"}
        if name == "mcp.servers.approve":
            for server in self.servers:
                if server["id"] == p["id"]:
                    server["state"] = "running"
            return {"ok": True, "state": "running", "reason": ""}
        if name == "home.status":
            return {
                "connected": True,
                "reason": "",
                "ha_version": "2026.10",
                "host": "homeassistant.local",
                "port": 8123,
                "areas_available": True,
                "token_present": True,
            }
        if name == "home.list":
            return {
                "ok": True,
                "connected": True,
                "reason": "",
                "areas": ["Wohnzimmer", "Küche"],
                "areas_available": True,
                "entities": [
                    {
                        "entity_id": "light.wz_decke",
                        "name": "Deckenlampe",
                        "area": "Wohnzimmer",
                        "state": "on",
                        "attributes": {"brightness": 102},
                    },
                    {
                        "entity_id": "light.wz_steh",
                        "name": "Stehlampe",
                        "area": "Wohnzimmer",
                        "state": "off",
                        "attributes": {},
                    },
                    {
                        "entity_id": "switch.kaffee",
                        "name": "Kaffeemaschine",
                        "area": "Küche",
                        "state": "off",
                        "attributes": {},
                    },
                    {
                        "entity_id": "scene.abend",
                        "name": "Abend",
                        "area": "Wohnzimmer",
                        "state": "scening",
                        "attributes": {},
                    },
                ],
            }
        if name in ("home.light", "home.switch", "home.scene"):
            return {"ok": True, "connected": True}
        if name == "day.today":
            return {
                "date": _now()[:10],
                "configured": True,
                "events": [
                    {
                        "start": _now(3600)[:16],
                        "end": "",
                        "title": "Zahnarzt",
                        "location": "Springfield",
                        "all_day": False,
                        "source": "google",
                    }
                ],
                "reminders": [],
                "later": [],
                "calendars": [{"id": "google", "label": "Google", "configured": True, "error": ""}],
            }
        if name == "stream.session.status":
            return {
                "active": self.mode == "stream",
                "session_id": "s1" if self.mode == "stream" else None,
                "scene": "Gameplay" if self.mode == "stream" else None,
                "plugins": {"obs": "connected", "twitch": "connected"},
            }
        if name == "stream.funken.top":
            return {"viewers": []}
        if name == "stream.overlay.status":
            return {
                "available": True,
                "configured": self.overlay,
                "credential": {"created_at": _now()} if self.overlay else None,
            }
        if name == "stream.overlay.setup":
            self.overlay = True
            return {"ok": True, "input_name": "Nox Overlay", "scene": "Gameplay"}
        if name == "mode.set":
            self.mode = p["mode"]
            self.push(
                "state.changed", {"path": "assistant.mode", "old": "companion", "new": self.mode}
            )
            return {
                "ok": True,
                "mode": self.mode,
                "profile": "stream" if self.mode == "stream" else "companion",
            }
        if name == "privacy.set":
            self.privacy = p.get("mode", self.privacy)
            self.push("privacy.mode_changed", {"current": self.privacy})
            return {"mode": self.privacy}
        if name == "voice.mute":
            return {"ok": True}
        if name == "security.kill":
            self.level = "safe_mode"
            self.push("security.kill_switch", {"origin": "ui", "reason": p.get("reason", "")})
            return {"ok": True, "report": {}}
        if name == "security.resume":
            self.level = "running"
            self.push("system.started", {"resumed": True})
            return {"ok": True}
        if name == "chat.send":
            return {
                "request_id": "r1",
                "text": "Morgen wird es sonnig, 18 Grad.",
                "provider": "ai.rules",
                "degraded": False,
            }
        if name in ("views.list",):
            return {"views": []}
        if name == "clip.list":
            return {"clips": []}
        if name == "consent.list":
            return {"consents": []}
        return None


def _install(page: Page, stub: StubCore) -> None:
    dist = DIST.resolve()

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
        stub.ws = ws

        def on_message(message: str | bytes) -> None:
            env = json.loads(message)
            name, corr, payload = env["name"], env["id"], env.get("payload", {})
            if name == "ipc.auth":
                ws.send(_envelope("response", name, {"ok": True, "session_id": "e2e"}, corr))
                return
            result = stub.answer(name, payload)
            if result is None:
                ws.send(
                    _envelope(
                        "error",
                        name,
                        {"code": "not_found", "message": f"unknown request {name}"},
                        corr,
                    )
                )
            else:
                ws.send(_envelope("response", name, result, corr))

        ws.on_message(on_message)

    page.route(f"{ORIGIN}/dashboard/**", serve)
    page.route_web_socket(WS_URL, on_socket)


def _artifacts(output_path: str) -> Path:
    base = Path(
        os.environ.get("NOX_UX_ARTIFACTS") or os.environ.get("NOX_E2E_ARTIFACTS") or output_path
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class Journey:
    page: Page
    stub: StubCore
    size: str
    scheme: str
    shots: Path
    _selector: str = ""

    def shot(self, step: str) -> None:
        self.page.screenshot(
            path=str(self.shots / f"{self.size}_{self.scheme}_{step}.png"), full_page=False
        )

    def axe(self, step: str) -> None:
        """Zero axe-core findings (WCAG 2.0/2.1/2.2 A and AA) on what is on screen now."""
        if not AXE.is_file():
            pytest.skip("axe-core is not installed (npm ci in ui/dashboard)")
        self.page.add_script_tag(path=str(AXE))
        result = self.page.evaluate(
            """async () => {
                const r = await axe.run(document, { runOnly: { type: 'tag',
                  values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] } });
                return r.violations.map(
                  (v) => v.id + ': ' + v.nodes.map((n) => n.target.join(' ')).join(' | '));
            }"""
        )
        assert result == [], f"axe at {step} ({self.size}, {self.scheme}): {result}"

    def gutter(self, container: Locator | None, what: str) -> None:
        """Text and controls keep the page gutter (16 px) from the edges of a sheet, or of the
        screen for a form in the page - nothing sits flush against the side."""
        box = (
            container.bounding_box()
            if container is not None
            else {"x": 0, "width": self.page.viewport_size["width"]}  # type: ignore[index]
        )
        assert box is not None, what
        edges = self.page.evaluate(
            """([sel, left, right]) => {
                const root = sel ? document.querySelector(sel) : document.querySelector('main');
                let min = Infinity, max = -Infinity, minEl = '', maxEl = '';
                for (const el of root.querySelectorAll('button, p, h2, h3, label, input, a')) {
                  const r = el.getBoundingClientRect();
                  if (r.width <= 1 || r.height <= 1) continue; // hidden for the eye (sr-only)
                  if (getComputedStyle(el).visibility === 'hidden') continue;
                  // Stopp sits at the header's spot, outside the sheet on purpose.
                  if (el.classList.contains('stop--corner')) continue;
                  if (r.bottom < 0 || r.top > innerHeight) continue;
                  const text = (el.textContent || '').slice(0, 30);
                  const name = el.tagName + '.' + el.className + ' ' + text;
                  if (r.left < min) { min = r.left; minEl = name; }
                  if (r.right > max) { max = r.right; maxEl = name; }
                }
                return [min - left, right - max, minEl, maxEl];
            }""",
            [
                None if container is None else self._selector,
                box["x"],
                box["x"] + box["width"],
            ],
        )
        assert edges[0] >= 16 and edges[1] >= 16, (
            f"{what} ({self.size}, {self.scheme}): {edges[0]:.0f}px left ({edges[2]}), "
            f"{edges[1]:.0f}px right ({edges[3]})"
        )

    def no_sideways_scroll(self) -> None:
        overflow = self.page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"page scrolls sideways by {overflow}px"

    def area(self, name: str) -> None:
        label = {"devices": "Geräte" if self.size == "phone" else "Geräte & Fähigkeiten"}.get(
            name, name
        )
        # The side bar and the bottom bar are both "Bereiche"; only one of them is shown per size.
        self.page.get_by_role("navigation", name="Bereiche").get_by_role(
            "link", name=label, exact=True
        ).click()


@pytest.fixture(scope="module")
def dashboard_built() -> None:
    if not (DIST / "index.html").is_file():
        pytest.skip("ui/dashboard/dist is not built (npm run build in ui/dashboard)")


@pytest.fixture
def journey(
    browser: Browser, output_path: str, request: pytest.FixtureRequest
) -> Iterator[Journey]:
    size, scheme = request.param
    base = _artifacts(output_path)
    videos = base / "videos"
    shots = base / "screens"
    shots.mkdir(parents=True, exist_ok=True)
    raw = base / "_raw" / f"{size}-{scheme}"
    context = browser.new_context(
        viewport=SIZES[size],
        record_video_dir=str(raw),
        record_video_size=SIZES[size],
        locale="de-DE",
        color_scheme=scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    stub = StubCore()
    _install(page, stub)
    try:
        yield Journey(page, stub, size, scheme, shots)
    finally:
        video = page.video
        context.close()  # finishes the video file
        if video is not None:
            videos.mkdir(parents=True, exist_ok=True)
            shutil.move(video.path(), videos / f"journey_{size}_{scheme}.webm")
        shutil.rmtree(raw, ignore_errors=True)


CASES = [(size, scheme) for size in SIZES for scheme in SCHEMES]


@pytest.mark.usefixtures("dashboard_built")
@pytest.mark.parametrize("journey", CASES, indirect=True, ids=[f"{s}-{c}" for s, c in CASES])
def test_household_journey(journey: Journey) -> None:  # noqa: PLR0915 - one journey, step by step
    page, stub = journey.page, journey.stub
    page.goto(f"{ORIGIN}/dashboard/?ws={WS_PORT}&lang=de#token=e2e-token")
    expect(page.locator(".app[data-conn=online]")).to_have_count(1, timeout=UI_TIMEOUT_MS)

    # 1. First run: name, privacy (Standard preselected), voice.
    first = page.get_by_role("dialog", name="Wie heißt du?")
    expect(first).to_be_visible(timeout=UI_TIMEOUT_MS)
    journey.shot("01-first-run")
    first.get_by_label("Dein Name").fill("Mila")
    page.get_by_role("button", name="Weiter").click()
    expect(page.get_by_role("radio", name=re.compile("^Standard"))).to_be_checked()
    page.get_by_role("button", name="Weiter").click()
    page.get_by_role("radio", name=re.compile("Supertonic", re.I)).check()
    page.get_by_role("button", name="Fertig").click()
    expect(page.locator(".first")).to_have_count(0, timeout=UI_TIMEOUT_MS)
    expect(page.get_by_role("heading", level=1, name=re.compile(r"Mila\.$"))).to_be_visible(
        timeout=UI_TIMEOUT_MS
    )
    assert stub.values["identity.user_display_name"] == "Mila"
    assert stub.values["voice.tts.engine"] == "supertonic"
    expect(page.get_by_role("region", name="Zuhause")).to_be_visible()
    journey.shot("02-heute")
    # Dein Tag: time and place are words, not code.
    place_font = page.evaluate(
        "() => getComputedStyle(document.querySelector('.day-list .detail-sub')).fontFamily"
    )
    assert "mono" not in place_font.lower(), place_font
    journey.axe("heute")
    journey.no_sideways_scroll()

    # 2. Ask Nox something: the field opens the command line, the last entry asks Nox.
    page.get_by_label("Sag Nox, was er tun soll").first.click()
    palette = page.get_by_role("dialog", name="Befehlszeile")
    expect(palette).to_be_visible()
    combo = palette.get_by_role("combobox")
    combo.fill("Wie wird das Wetter morgen?")
    journey.shot("03-palette")
    journey.axe("palette")
    combo.press("End")
    expect(palette.get_by_role("option", name=re.compile("Nox fragen"))).to_have_attribute(
        "aria-selected", "true"
    )
    combo.press("Enter")
    expect(page.get_by_text("Morgen wird es sonnig, 18 Grad.")).to_be_visible(timeout=UI_TIMEOUT_MS)
    journey.shot("04-gespraech")

    # 3a. A permission question in the conversation / on Heute: allow it inline.
    journey.area("Heute")
    stub.ask_permission("p1", "process_start", "OBS Studio")
    card = page.get_by_role("region", name="Darf Nox ein Programm starten?")
    expect(card).to_be_visible(timeout=UI_TIMEOUT_MS)
    expect(card.get_by_text(re.compile(r"Noch \d+ Sekunden"))).to_be_visible()
    journey.shot("05-frage-heute")
    journey.axe("frage-heute")
    card.get_by_role("button", name="Erlauben", exact=True).click()
    expect(page.get_by_text("Erlaubt. Nox macht es jetzt.")).to_be_visible()
    assert (
        "security.permission.reply",
        {"grant_id": "p1", "decision": "allow", "remember": False},
    ) in stub.requests

    # 3b. On any other page it is a sheet: deny it there.
    journey.area("devices")
    stub.ask_permission("p2", "delete", "Archiv/alt.zip", risk="high")
    sheet = page.get_by_role("alertdialog", name="Darf Nox etwas löschen?")
    expect(sheet).to_be_visible(timeout=UI_TIMEOUT_MS)
    journey.shot("06-frage-sheet")
    journey._selector = ".perm-dialog"
    journey.gutter(page.locator(".perm-dialog"), "permission sheet")
    sheet.get_by_role("button", name="Ablehnen").click()
    expect(page.get_by_text("Abgelehnt. Nox tut es nicht.")).to_be_visible()

    # 4. Pair a phone: the empty list says what pairing is for and opens the pairing sheet.
    expect(page.get_by_text("Noch kein Gerät gekoppelt.")).to_be_visible()
    page.get_by_role("button", name="Handy koppeln").click()
    pair_sheet = page.get_by_role("dialog", name="Neues Gerät koppeln")
    expect(pair_sheet).to_be_visible()
    page.get_by_label("Name des Geräts (freiwillig)").fill("Handy")
    page.get_by_role("button", name="Code erzeugen").click()
    expect(page.get_by_role("img", name=re.compile("QR-Code zum Koppeln"))).to_be_visible(
        timeout=UI_TIMEOUT_MS
    )
    journey.shot("07-koppeln")
    journey._selector = "dialog[open]"
    journey.gutter(pair_sheet, "pairing sheet")
    journey.axe("koppeln")
    stub.devices.append(
        {
            "id": "d1",
            "name": "Handy",
            "paired_at": _now(),
            "last_seen_at": _now(),
            "revoked_at": None,
        }
    )
    stub.push("remote.paired", {"channel": "lan", "name": "Handy"})
    expect(page.get_by_text("Handy ist gekoppelt.")).to_be_visible(timeout=UI_TIMEOUT_MS)
    expect(pair_sheet).to_have_count(0)
    journey.no_sideways_scroll()

    # 5. Add an extension under Fähigkeiten › Für Fortgeschrittene.
    page.get_by_role("tab", name="Fähigkeiten").click()
    page.get_by_role("button", name="Fähigkeit hinzufügen").click()
    form = page.locator("form").filter(has=page.get_by_label("Kurzname"))
    form.get_by_label("Adresse im Netz").check()
    form.get_by_label("Name", exact=True).fill("Wetterdienst")
    form.get_by_label("Kurzname").fill("wetter")
    form.get_by_label("Adresse (https)").fill("https://wetter.example/mcp")
    form.get_by_label("Version").fill("2.0.0")
    journey.shot("08-erweiterung")
    journey.gutter(None, "extension form")
    form.get_by_role("button", name="Hinzufügen", exact=True).click()
    server = page.get_by_role("region", name="Wetterdienst")
    expect(server.get_by_text("wartet auf Erlaubnis")).to_be_visible(timeout=UI_TIMEOUT_MS)
    server.get_by_role("button", name="Ersten Start erlauben").click()
    expect(server.get_by_text("läuft", exact=True)).to_be_visible(timeout=UI_TIMEOUT_MS)
    journey.axe("faehigkeiten")

    # 6. Change the Modus from the header: Heute becomes the live view.
    page.locator(".presence").click()
    menu = page.get_by_role("dialog", name="Modus")
    expect(menu).to_be_visible()
    menu.get_by_role("radio", name=re.compile("^Stream")).check()
    journey.shot("09-modus")
    journey._selector = "dialog[open]"
    journey.gutter(menu, "Modus menu")
    journey.axe("modus")
    menu.get_by_role("button", name="Zu „Stream“ wechseln").click()
    expect(menu.get_by_text(re.compile("Jetzt: Stream-Modus"))).to_be_visible(timeout=UI_TIMEOUT_MS)
    menu.get_by_role("button", name="Schließen").click()
    journey.area("Heute")
    expect(page.get_by_role("button", name="Privatsphäre-Szene auslösen")).to_be_visible(
        timeout=UI_TIMEOUT_MS
    )
    journey.shot("10-live")

    # 7. Set the stream up: Einstellungen › Stream, the overlay step.
    journey.area("Einstellungen")
    page.get_by_role("link", name=re.compile("^Stream")).click()
    page.get_by_role("button", name="OBS-Overlay einrichten").click()
    expect(page.get_by_text("Fertig: „Nox Overlay“ in der Szene „Gameplay“.")).to_be_visible(
        timeout=UI_TIMEOUT_MS
    )
    journey.shot("11-stream-einrichten")
    journey.axe("stream-einrichten")

    # 8. Stopp acts at once; Fortsetzen brings Nox back.
    page.get_by_role("button", name="Stopp", exact=True).click()
    strip = page.locator(".strip--danger")
    expect(strip).to_contain_text("Nox ist gestoppt.", timeout=UI_TIMEOUT_MS)
    expect(page.get_by_role("button", name="Stopp", exact=True)).to_have_count(0)
    expect(page.get_by_label("Sag Nox, was er tun soll").first).to_be_disabled()
    journey.shot("12-gestoppt")
    journey.axe("gestoppt")
    page.get_by_role("button", name="Fortsetzen").click()
    expect(page.get_by_role("button", name="Stopp", exact=True)).to_be_visible(
        timeout=UI_TIMEOUT_MS
    )
    expect(page.locator(".strip--danger")).to_have_count(0)
    assert stub.names().count("security.kill") == 1
    assert "security.resume" in stub.names()
    journey.shot("13-fortgesetzt")

    # 9. The remaining pages, for the record (screenshots) and for axe in a real browser.
    journey.area("Nox")
    page.get_by_role("tab", name="Verlauf").click()
    expect(page.get_by_role("group", name="Zeigen")).to_be_visible()
    journey.shot("14-verlauf")
    journey.axe("verlauf")
    journey.area("Einstellungen")
    page.get_by_role("link", name="Einstellungen", exact=True).first.click()
    expect(page.get_by_role("heading", level=1, name="Einstellungen")).to_be_visible()
    journey.shot("15-einstellungen")
    journey.axe("einstellungen")
    page.get_by_role("link", name=re.compile("^Privatsphäre und Sicherheit")).click()
    expect(page.get_by_role("heading", level=1, name="Privatsphäre und Sicherheit")).to_be_visible()
    journey.shot("16-privatsphaere")
    journey.axe("privatsphaere")
    journey.no_sideways_scroll()


STOP_CASES = [("phone", "light"), ("desktop", "dark")]


@pytest.mark.usefixtures("dashboard_built")
@pytest.mark.parametrize(
    "journey", STOP_CASES, indirect=True, ids=[f"stop-{s}-{c}" for s, c in STOP_CASES]
)
def test_stop_is_never_behind_a_sheet(journey: Journey) -> None:
    """Stopp works during the first run, with a question open and with a menu open."""
    page, stub = journey.page, journey.stub
    page.goto(f"{ORIGIN}/dashboard/?ws={WS_PORT}&lang=de#token=e2e-token")
    expect(page.locator(".app[data-conn=online]")).to_have_count(1, timeout=UI_TIMEOUT_MS)

    def stop_in(surface: Locator, step: str) -> None:
        button = surface.get_by_role("button", name="Stopp", exact=True)
        expect(button).to_be_visible()
        box = button.bounding_box()
        assert box is not None
        width = SIZES[journey.size]["width"]
        assert box["x"] >= 0 and box["x"] + box["width"] <= width, f"{step}: Stopp off screen"
        # At the header's Stopp spot, within the gutter.
        header = page.locator("header.top .stop:not(.stop--corner)").bounding_box()
        assert header is not None
        assert abs(header["x"] - box["x"]) <= 2 and abs(header["y"] - box["y"]) <= 2, (
            f"{step}: corner Stopp {box} is not over the header's {header}"
        )
        assert box["x"] + box["width"] <= width - 16, f"{step}: Stopp in the gutter"
        journey.shot(f"stop-{step}")
        before = stub.names().count("security.kill")
        button.click()
        expect(surface.get_by_role("button", name="Stopp", exact=True)).to_have_count(0)
        assert stub.names().count("security.kill") == before + 1, step

    def resume() -> None:
        page.get_by_role("button", name="Fortsetzen").click()
        expect(page.get_by_role("button", name="Stopp", exact=True)).to_be_visible(
            timeout=UI_TIMEOUT_MS
        )

    # 1. During the first run.
    first = page.get_by_role("dialog", name="Wie heißt du?")
    expect(first).to_be_visible(timeout=UI_TIMEOUT_MS)
    stop_in(first, "first-run")
    page.get_by_role("button", name="Später einrichten").click()
    resume()

    # 2. With a question open as a sheet (the page behind it is inert).
    journey.area("Einstellungen")
    stub.ask_permission("s1", "delete", "Archiv/alt.zip", risk="high")
    sheet = page.get_by_role("alertdialog", name="Darf Nox etwas löschen?")
    expect(sheet).to_be_visible(timeout=UI_TIMEOUT_MS)
    stop_in(sheet, "question")
    # The core ends open questions on Stopp.
    stub.push(
        "security.permission_decided",
        {"request_id": "s1", "decision": "deny", "rule_id": "kill", "by": "policy"},
    )
    expect(sheet).to_have_count(0, timeout=UI_TIMEOUT_MS)
    resume()

    # 3. With the Modus menu open (a native modal dialog).
    page.locator(".presence").click()
    menu = page.get_by_role("dialog", name="Modus")
    expect(menu).to_be_visible()
    stop_in(menu, "menu")
    journey.axe("stop-in-menu")
