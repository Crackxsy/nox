"""Click test, on video, of what a household member does with wave C: pair a PC, switch on
"Befehle ausführen" on that PC, and answer a permission question while Nox controls its mouse.

Same harness as `test_devices_stubbed.py` (the built bundle through `page.route`, the IPC socket
answered by an in-test stub - nothing leaves the browser). German and English, phone and desktop:

1. Geräte › Computer und Server › "PC oder Laptop hinzufügen": the install step comes first, the
   code only after "Installer läuft ... Code erzeugen"; "Link kopieren" copies the link alone; the
   status flips to "verbunden" when the stub reports the new PC.
2. The PC's sheet: "Befehle ausführen" is a lock, not a switch; "So schaltest du es ein" names the
   Windows Command Prompt and `nox-node allow shell`. When the device then allows it (the stub
   flips it and tells the page), the row is a switch that is on.
3. Nox controls the PC's mouse: the banner names it; a permission question names the PC in bold,
   shows the target exactly as sent, and asks for the PIN. The core refuses the first answer with
   the sub-reason `node_session_linked`: the page says so in its own words (never "Die PIN stimmt
   nicht"); Ablehnen goes through.

After the main steps the page is checked with axe-core (zero findings) and for sideways scrolling.
Artifacts: `videos/nodes_<lang>_<size>.webm` and `screens/<size>_<lang>_nodes_<step>.png` under
`NOX_E2E_ARTIFACTS` (uploaded as `e2e-clicktests`). All names and addresses are made up.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.test_devices_stubbed import NodeStub, _node
from tests.e2e.test_ux_redesign_stubbed import (
    DIST,
    ORIGIN,
    SIZES,
    UI_TIMEOUT_MS,
    WS_PORT,
    Journey,
    StubError,
    _artifacts,
    _install,
    _now,
)

pytestmark = pytest.mark.e2e

PC = "a" * 32
CASES = [(lang, size) for lang in ("de", "en") for size in ("phone", "desktop")]

#: The words the journey looks for, per language (copied from the dictionaries on purpose: a
#: changed sentence must fail here, where somebody reads it).
WORDS = {
    "de": {
        "section": "Computer und Server",
        "add": "PC oder Laptop hinzufügen",
        "ready": "Installer läuft und fragt nach dem Link – Code erzeugen",
        "copy": "Link kopieren",
        "connected": "Laptop ist verbunden.",
        "open": "Laptop öffnen",
        "shell": "Befehle ausführen",
        "how": "So schaltest du es ein",
        "cmd": (
            "Öffne auf Laptop die Eingabeaufforderung (Windows-Taste, „cmd“ tippen) und gib ein:"
        ),
        "banner": "Nox steuert gerade Maus und Tastatur auf Laptop.",
        "question": "Darf Nox etwas löschen?",
        "sentence": "Nox möchte auf Laptop löschen: Laptop (aaaa): Archiv/neu.zip",
        "pin": "Sicherheits-PIN",
        "allow": "Erlauben",
        "deny": "Ablehnen",
        "linked": "Nox steuert gerade diesen PC. Von hier geht Erlauben nicht",
        "denied": "Abgelehnt. Nox tut es nicht.",
        "close": "Schließen",
    },
    "en": {
        "section": "Computers and servers",
        "add": "Add a PC or laptop",
        "ready": "The installer runs and asks for the link – make a code",
        "copy": "Copy link",
        "connected": "Laptop is connected.",
        "open": "Open Laptop",
        "shell": "Run commands",
        "how": "How to switch it on",
        "cmd": "On Laptop, open the Command Prompt (Windows key, type “cmd”) and enter:",
        "banner": "Nox is controlling mouse and keyboard on Laptop.",
        "question": "May Nox delete something?",
        "sentence": "On Laptop, Nox wants to delete: Laptop (aaaa): Archiv/neu.zip",
        "pin": "Security PIN",
        "allow": "Allow",
        "deny": "Deny",
        "linked": "Nox is controlling this PC right now. Allowing does not work from here",
        "denied": "Denied. Nox will not do it.",
        "close": "Close",
    },
}


@dataclass
class JourneyStub(NodeStub):
    """No PC at first; a PIN is set; Nox can be made to control the PC's mouse."""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    refuse_first_reply: bool = True

    def answer(self, name: str, p: dict[str, Any]) -> dict[str, Any] | None:
        if name == "security.pin.status":
            self.requests.append((name, p))
            return {"configured": True}
        if name == "node.computer.status":
            self.requests.append((name, p))
            return {"sessions": self.sessions, "controlled_here": False}
        if name == "security.permission.reply" and self.refuse_first_reply and p.get("pin"):
            self.requests.append((name, p))
            self.refuse_first_reply = False
            raise StubError(
                "permission.denied",
                "Nox controls mouse and keyboard on the PC this browser runs on; nothing can be "
                "approved or changed from here until that ends - deny or stop",
                {"reason": "node_session", "detail": "node_session_linked"},
            )
        return super().answer(name, p)


@pytest.fixture(scope="module")
def dashboard_built() -> None:
    if not (DIST / "index.html").is_file():
        pytest.skip("ui/dashboard/dist is not built (npm run build in ui/dashboard)")


@pytest.fixture
def journey(
    browser: Browser, output_path: str, request: pytest.FixtureRequest
) -> Iterator[Journey]:
    lang, size = request.param
    base = _artifacts(output_path)
    shots = base / "screens"
    shots.mkdir(parents=True, exist_ok=True)
    raw = base / "_raw" / f"nodes-{lang}-{size}"
    context = browser.new_context(
        viewport=SIZES[size],
        record_video_dir=str(raw),
        record_video_size=SIZES[size],
        locale="de-DE" if lang == "de" else "en-US",
        color_scheme="light",
        reduced_motion="reduce",
    )
    context.add_init_script("window.localStorage.setItem('nox.firstRun', 'done')")
    # The page is served over plain http (not a secure context): no real clipboard there, so a
    # stand-in that keeps what was written.
    context.add_init_script(
        "window.__copied = '';"
        "Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {"
        " writeText: (t) => { window.__copied = t; return Promise.resolve(); } } });"
    )
    page = context.new_page()
    stub = JourneyStub()
    _install(page, stub)
    try:
        yield Journey(page, stub, size, f"{lang}_nodes", shots)
    finally:
        video = page.video
        context.close()  # finishes the video file
        if video is not None:
            (base / "videos").mkdir(parents=True, exist_ok=True)
            shutil.move(video.path(), base / "videos" / f"nodes_{lang}_{size}.webm")
        shutil.rmtree(raw, ignore_errors=True)


@pytest.mark.usefixtures("dashboard_built")
@pytest.mark.parametrize("journey", CASES, indirect=True, ids=[f"{lang}-{s}" for lang, s in CASES])
def test_pair_a_pc_switch_on_commands_and_answer_while_nox_controls_the_mouse(  # noqa: PLR0915
    journey: Journey, request: pytest.FixtureRequest
) -> None:
    lang, _ = request.node.callspec.params["journey"]
    w = WORDS[lang]
    page = journey.page
    stub = journey.stub
    assert isinstance(stub, JourneyStub)
    page.goto(f"{ORIGIN}/dashboard/?ws={WS_PORT}&lang={lang}#token=e2e-token")
    expect(page.locator(".app[data-conn=online]")).to_have_count(1, timeout=UI_TIMEOUT_MS)
    page.evaluate("() => { location.hash = '#/devices/devices'; }")
    section = page.get_by_role("region", name=w["section"])
    expect(section).to_be_visible(timeout=UI_TIMEOUT_MS)

    # 1. Add a PC: the install step is on screen before there is any code.
    section.get_by_role("button", name=w["add"]).click()
    add = page.get_by_role("dialog", name=w["add"])
    expect(add).to_be_visible()
    expect(add.get_by_text(re.compile(r"Nox-Setup-<(Version|version)>\.exe"))).to_be_visible()
    assert not [n for n in stub.names() if n == "node.pair.start"]
    journey.shot("01-installieren")
    journey.axe("installieren")
    add.get_by_role("button", name=w["ready"]).click()
    expect(add.get_by_role("img", name=re.compile("QR"))).to_be_visible(timeout=UI_TIMEOUT_MS)
    link = add.get_by_text(re.compile(r"^nox-node://pair\?"))
    expect(link).to_be_visible()
    add.get_by_role("button", name=w["copy"]).click()
    copied = page.evaluate("() => window.__copied")
    assert copied.startswith("nox-node://pair?") and '"' not in copied  # the link alone
    journey.shot("02-code")
    journey.axe("code")
    journey.no_sideways_scroll()
    stub.nodes.append(_node(PC, "Laptop", groups=[_group("system", True), _group("shell", False)]))
    expect(add.get_by_text(w["connected"])).to_be_visible(timeout=UI_TIMEOUT_MS)
    journey.shot("03-verbunden")
    add.get_by_role("button", name=w["close"], exact=True).click()

    # 2. "Befehle ausführen" is a lock; the command fits the device; once the device allows it,
    #    the row is a switch.
    section.get_by_role("button", name=w["open"]).click()
    sheet = page.get_by_role("dialog", name="Laptop")
    expect(sheet.get_by_role("switch", name=w["shell"])).to_have_count(0)
    sheet.get_by_role("button", name=w["how"]).click()
    expect(sheet.get_by_text(w["cmd"])).to_be_visible()
    expect(sheet.get_by_text("nox-node allow shell", exact=True)).to_be_visible()
    journey.shot("04-befehl")
    journey.axe("befehl")
    journey.no_sideways_scroll()
    stub.nodes[0]["groups"] = [_group("system", True), _group("shell", True)]
    stub.push("remote.paired", {"channel": "node", "name": "Laptop"})
    switch = sheet.get_by_role("switch", name=w["shell"])
    expect(switch).to_have_attribute("aria-checked", "true", timeout=UI_TIMEOUT_MS)
    journey.shot("05-an")
    sheet.get_by_role("button", name=w["close"], exact=True).click()

    # 3. Nox controls the PC's mouse; a high-risk question names the PC and needs the PIN.
    session = {
        "active": True,
        "reason": "",
        "stop": "ctrl+alt+shift+k",
        "device": "Laptop",
        "device_id": PC,
    }
    stub.sessions = [session]
    stub.push("desktop.computer_use", session)
    expect(page.get_by_text(w["banner"], exact=False)).to_be_visible(timeout=UI_TIMEOUT_MS)
    stub.push(
        "security.permission_requested",
        {
            "request_id": "n1",
            "agent": "companion",
            "tool": "file",
            "action": "delete",
            "mode": "companion",
            "risk": "high",
            "target": "Laptop (aaaa): Archiv/neu.zip",
            "device_id": PC,
            "device_name": "Laptop",
            "expires_at": _now(55),
            "remember_ttl_s": 0,
        },
    )
    prompt = page.get_by_role("alertdialog", name=w["question"])
    expect(prompt).to_be_visible(timeout=UI_TIMEOUT_MS)
    expect(prompt.get_by_text(w["sentence"], exact=True)).to_be_visible()
    expect(prompt.locator("strong", has_text="Laptop")).to_be_visible()
    journey.shot("06-frage")
    journey.axe("frage")
    prompt.get_by_label(w["pin"]).fill("2468")
    prompt.get_by_role("button", name=w["allow"], exact=True).click()
    expect(prompt.get_by_role("alert")).to_contain_text(w["linked"])
    # Not the PIN message and not the generic "reply did not arrive" line.
    assert not re.search(r"stimmt nicht|nicht angekommen|did not arrive", prompt.inner_text())
    journey.shot("07-abgelehnt-von-hier")
    prompt.get_by_role("button", name=w["deny"], exact=True).click()
    expect(page.get_by_text(w["denied"])).to_be_visible(timeout=UI_TIMEOUT_MS)
    journey.no_sideways_scroll()


def _group(name: str, on_device: bool) -> dict[str, Any]:
    return {"group": name, "enabled_on_device": on_device, "narrowed": False, "active": on_device}
