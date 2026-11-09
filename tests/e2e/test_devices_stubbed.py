"""Click test of Geräte › Computer und Server (spec C §1.10) against a stubbed IPC, on video.

Same harness as `test_ux_redesign_stubbed.py`: the built bundle is served through `page.route`
and the IPC websocket is answered in the test through `page.route_web_socket` - no port is
opened, nothing leaves the browser. The stub keeps the nodes' state, so every click has a visible
consequence.

One journey per size (phone, tablet, desktop) and colour scheme: the list of computers -> a
computer's sheet -> switch a group off (narrowing, no PIN) -> try to switch on a group the device
does not allow (the page says it only works on the device, and sends nothing) -> set where a tool
family runs by default -> add a Linux laptop (code, QR, three steps; the status flips to
"verbunden" when the stub reports the new node) -> remove a computer with the second press.
After the main steps the page is checked with axe-core (WCAG 2.2 AA rules, zero findings) and for
sideways scrolling; every step is a screenshot.

Artifacts go where the other click tests put theirs (`NOX_E2E_ARTIFACTS` in CI, uploaded as
`e2e-clicktests`): `videos/devices_<size>_<scheme>.webm` and
`screens/<size>_<scheme>_devices_<step>.png`.
All names and addresses are made up.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.test_ux_redesign_stubbed import (
    CASES,
    ORIGIN,
    QR_SVG,
    SIZES,
    UI_TIMEOUT_MS,
    WS_PORT,
    Journey,
    StubCore,
    _artifacts,
    _install,
    _now,
)

pytestmark = pytest.mark.e2e

PC = "a" * 32
PI = "b" * 32
LAPTOP = "c" * 32
LONG = "d" * 32
#: Forty characters, no space: must wrap, never widen the page.
LONG_NAME = "Wohnzimmerrechnerimkellerdesnachbarhauses"


def _node(node_id: str, name: str, **over: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "name": name,
        "label": f"{name} ({node_id[:4]})",
        "online": True,
        "compatible": True,
        "os": "windows",
        "arch": "AMD64",
        "version": "0.7.0",
        "paired_at": _now(-86400),
        "last_seen_at": _now(),
        "revoked": False,
        "tools": ["system.info", "notify.show"],
        "linked_device_id": None,
        "groups": [
            {"group": "system", "enabled_on_device": True, "narrowed": False, "active": True},
            {"group": "notify", "enabled_on_device": True, "narrowed": False, "active": True},
            {"group": "shell", "enabled_on_device": False, "narrowed": False, "active": False},
        ],
    }
    node.update(over)
    return node


@dataclass
class NodeStub(StubCore):
    nodes: list[dict[str, Any]] = field(
        default_factory=lambda: [
            _node(PC, "Arbeits-PC"),
            _node(PI, "Wohnzimmer-Pi", os="linux", arch="aarch64", online=False, compatible=None),
            _node(LONG, LONG_NAME, online=False, compatible=None),
        ]
    )
    defaults: dict[str, str] = field(default_factory=lambda: {"system": "auto", "notify": "origin"})

    def _defaults(self) -> list[dict[str, Any]]:
        tools = {"system": ["system.info"], "notify": ["notify.show"]}
        return [
            {"group": g, "tools": tools[g], "device": d, "raw": d} for g, d in self.defaults.items()
        ]

    def answer(self, name: str, p: dict[str, Any]) -> dict[str, Any] | None:
        if name == "node.list":
            self.requests.append((name, p))
            return {"enabled": True, "nodes": self.nodes, "defaults": self._defaults()}
        if name == "node.narrow":
            self.requests.append((name, p))
            node = next(n for n in self.nodes if n["id"] == p["node_id"])
            for group in node["groups"]:
                if group["group"] == p["group"]:
                    group["narrowed"] = p["narrowed"]
                    group["active"] = group["enabled_on_device"] and not p["narrowed"]
            return {"ok": True, "node_id": node["id"], "groups": node["groups"]}
        if name == "node.defaults.set":
            self.requests.append((name, p))
            self.defaults[p["group"]] = p["device"]
            return {"ok": True, "defaults": self._defaults()}
        if name == "node.pair.start":
            self.requests.append((name, p))
            return {
                "code": "K7F29QAB",
                "expires_at": _now(300),
                "ttl_s": 300,
                "link": "nox-node://pair?h=192.168.1.20&p=47843&c=K7F29QAB&fp=" + "ab" * 32,
                "links": [],
                "qr": QR_SVG,
                "ca_fingerprint": "12:34:56:78:9A:BC:DE:F0",
            }
        if name == "node.unpair":
            self.requests.append((name, p))
            self.nodes = [n for n in self.nodes if n["id"] != p["device_id"]]
            return {"ok": True, "device_id": p["device_id"], "was_online": False}
        return super().answer(name, p)


@pytest.fixture(scope="module")
def dashboard_built() -> None:
    from tests.e2e.test_ux_redesign_stubbed import DIST

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
    raw = base / "_raw" / f"devices-{size}-{scheme}"
    context = browser.new_context(
        viewport=SIZES[size],
        record_video_dir=str(raw),
        record_video_size=SIZES[size],
        locale="de-DE",
        color_scheme=scheme,
        reduced_motion="reduce",
    )
    # Past the first run: this journey is about the devices page.
    context.add_init_script("window.localStorage.setItem('nox.firstRun', 'done')")
    page = context.new_page()
    stub = NodeStub()
    _install(page, stub)
    try:
        yield Journey(page, stub, size, f"{scheme}_devices", shots)
    finally:
        video = page.video
        context.close()  # finishes the video file
        if video is not None:
            videos.mkdir(parents=True, exist_ok=True)
            shutil.move(video.path(), videos / f"devices_{size}_{scheme}.webm")
        shutil.rmtree(raw, ignore_errors=True)


def _sent(stub: StubCore, name: str) -> list[dict[str, Any]]:
    return [p for n, p in stub.requests if n == name]


@pytest.mark.usefixtures("dashboard_built")
@pytest.mark.parametrize("journey", CASES, indirect=True, ids=[f"{s}-{c}" for s, c in CASES])
def test_computers_and_what_nox_may_do_there(journey: Journey) -> None:  # noqa: PLR0915
    page = journey.page
    stub = journey.stub
    assert isinstance(stub, NodeStub)
    page.goto(f"{ORIGIN}/dashboard/?ws={WS_PORT}&lang=de#token=e2e-token")
    expect(page.locator(".app[data-conn=online]")).to_have_count(1, timeout=UI_TIMEOUT_MS)

    # 1. The list: above the phones, one row per computer, in plain words.
    journey.area("devices")
    section = page.get_by_role("region", name="Computer und Server")
    expect(section).to_be_visible(timeout=UI_TIMEOUT_MS)
    expect(
        section.get_by_text("Nox darf hier: Systemwerte lesen, Hinweise zeigen.").first
    ).to_be_visible()
    expect(section.get_by_text("Verbunden", exact=True)).to_be_visible()
    expect(section.get_by_text(re.compile(r"^Offline seit ")).first).to_be_visible()
    expect(section.get_by_text(LONG_NAME, exact=True)).to_be_visible()
    journey.shot("01-computer")
    journey.axe("computer")
    journey.no_sideways_scroll()

    # 2. A computer's sheet: switch "Hinweise zeigen" off - narrowing needs no PIN here.
    section.get_by_role("button", name="Arbeits-PC öffnen").click()
    sheet = page.get_by_role("dialog", name="Arbeits-PC")
    expect(sheet).to_be_visible()
    notify = sheet.get_by_role("switch", name="Hinweise zeigen")
    expect(notify).to_have_attribute("aria-checked", "true")
    notify.click()
    expect(notify).to_have_attribute("aria-checked", "false")
    expect(sheet.get_by_text("Hier ausgeschaltet.", exact=False)).to_be_visible()
    assert _sent(stub, "node.narrow") == [{"node_id": PC, "group": "notify", "narrowed": True}]

    # 3. A group the device does not allow: explained, never sent.
    sheet.get_by_role("switch", name="Befehle ausführen").click()
    expect(sheet.get_by_text("Das geht nur am Gerät selbst")).to_be_visible()
    expect(sheet.get_by_text("nox node allow shell")).to_be_visible()
    assert len(_sent(stub, "node.narrow")) == 1
    journey.shot("02-sheet")
    journey.axe("sheet")

    # 4. Where "Systemwerte lesen" runs when nobody names a device.
    sheet.get_by_label("Systemwerte lesen: Standardgerät").select_option(PC)
    assert _sent(stub, "node.defaults.set") == []  # choosing alone changes nothing
    sheet.get_by_role("button", name="Übernehmen: Systemwerte lesen").click()
    expect(sheet.get_by_text("Gespeichert.")).to_be_visible()
    assert _sent(stub, "node.defaults.set") == [{"group": "system", "device": PC}]
    journey.shot("03-standard")
    sheet.get_by_role("button", name="Schließen", exact=True).click()
    expect(section.get_by_text("Standard für: Systemwerte lesen")).to_be_visible()

    # 5. Add a Linux laptop: code, QR, steps - and the status flips when it connects.
    section.get_by_role("button", name="Weiteren Computer hinzufügen").click()
    add = page.get_by_role("dialog", name="PC oder Laptop hinzufügen")
    expect(add).to_be_visible()
    add.get_by_label("Linux").check()
    add.get_by_label("Name des Geräts (freiwillig)").fill("Laptop")
    add.get_by_role("button", name="Code erzeugen").click()
    expect(add.get_by_role("img", name=re.compile("QR-Code zum Verbinden"))).to_be_visible(
        timeout=UI_TIMEOUT_MS
    )
    expect(add.get_by_text("bash install-linux.sh --node")).to_be_visible()
    expect(add.get_by_text("Wartet auf das Gerät …")).to_be_visible()
    journey.shot("04-hinzufuegen")
    journey.axe("hinzufuegen")
    journey.no_sideways_scroll()
    assert _sent(stub, "node.pair.start") == [{"name": "Laptop"}]
    stub.nodes.append(_node(LAPTOP, "Laptop", os="linux", arch="x86_64"))
    expect(add.get_by_text("Laptop ist verbunden.")).to_be_visible(timeout=UI_TIMEOUT_MS)
    journey.shot("05-verbunden")
    add.get_by_role("button", name="Schließen", exact=True).click()

    # 6. A long name wraps in the list and in its sheet.
    section.get_by_role("button", name=f"{LONG_NAME} öffnen").click()
    long_sheet = page.get_by_role("dialog", name=LONG_NAME)
    expect(long_sheet).to_be_visible()
    journey.shot("06-langer-name")
    journey.no_sideways_scroll()
    long_sheet.get_by_role("button", name="Schließen", exact=True).click()

    # 7. Remove the Pi: the first press only arms it.
    section.get_by_role("button", name="Wohnzimmer-Pi öffnen").click()
    pi = page.get_by_role("dialog", name="Wohnzimmer-Pi")
    pi.get_by_role("button", name="Entfernen", exact=True).click()
    expect(
        pi.get_by_text(re.compile("Nox kann auf Wohnzimmer-Pi dann nichts mehr tun"))
    ).to_be_visible()
    assert _sent(stub, "node.unpair") == []
    journey.shot("07-entfernen")
    pi.get_by_role("button", name="Wirklich entfernen").click()
    expect(page.get_by_text("Wohnzimmer-Pi ist entfernt.")).to_be_visible()
    assert _sent(stub, "node.unpair") == [{"device_id": PI}]
    expect(section.get_by_role("button", name="Wohnzimmer-Pi öffnen")).to_have_count(0)
    journey.shot("08-entfernt")
    journey.axe("entfernt")
    journey.no_sideways_scroll()
