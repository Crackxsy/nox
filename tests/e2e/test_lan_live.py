"""Network access, clicked through end to end: a real core, real TLS, three real browser windows.

A PC (the local dashboard), a phone and a tablet - headless Chromium at their real sizes, each
recorded on video, with a screenshot at every step - go through what a household does:

  1. the PC makes a pairing code with a name; the phone opens the pairing link, leaves its own
     name field empty and pairs - and is listed under the name the PC gave it,
  2. the PC sees the phone appear in its list without reloading,
  3. Nox asks for permission at high risk; the prompt shows on the PC and the phone at once. With
     no security PIN set the phone cannot allow it (it says where that works) - the PC allows,
     and the phone is told another device decided,
  4. a PIN is set; the next high-risk question asks the phone for it, the phone allows with it,
     the PC is told, and the audit names the phone,
  5. the PC denies the next one; a fourth one runs out, and both say nobody answered,
  6. the tablet, never paired, says so; the PC - which now needs the PIN to make a code - makes
     one, and the tablet takes it typed,
  7. the PC removes the phone, and the phone says it was removed.

Along the way axe-core (from `ui/dashboard/node_modules`) checks every screen it passes for
serious and critical WCAG 2.2 A/AA violations, and the test fails on any.

The confirmation round trip runs over the real WebSocket IPC and the network bridge - the part no
unit test reaches. Artifacts go to `NOX_E2E_ARTIFACTS` (CI uploads them) or the test's tmp folder.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from nox.app import DASHBOARD_DIST, DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import NoxConfig, load_config
from nox.ipc.tokens import read_session_token
from nox.lan.install import LanRuntime
from nox.security.model import Decision, PermissionRequest, Risk
from tests._ports import free_port_base
from tests.e2e.test_dashboard_live import _ensure_ui_built

pytestmark = pytest.mark.e2e

T = TypeVar("T")
START_TIMEOUT_S = 60
UI_TIMEOUT_MS = 15000
PIN = "4711"

#: axe-core, pinned in the dashboard's devDependencies (`npm ci` in ui/dashboard provides it).
AXE_JS = DASHBOARD_DIST.parent / "node_modules" / "axe-core" / "axe.min.js"
#: What the dashboard promises: WCAG 2.2 A and AA.
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]

#: Name, viewport, user agent - what each window pretends to be.
DEVICES: dict[str, dict[str, Any]] = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    "phone": {
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
        ),
    },
    "tablet": {
        "viewport": {"width": 820, "height": 1180},
        "device_scale_factor": 2,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": (
            "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
        ),
    },
}


def _config(tmp_path: Path) -> NoxConfig:
    base = free_port_base()
    names = ("data", "vault", "index", "database", "cache", "backups", "runtime", "logs")
    overrides = {
        "paths": {f"{n}_dir": str(tmp_path / n) for n in names},
        "ipc": {
            "port": base,
            "http_port": base + 1,
            "lan": {"enabled": True, "host": "127.0.0.1", "port": 0},
        },
        "privacy": {"mode": "offline"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        # Secrets (the PIN set below) in the test's own folder, never the OS keyring of whoever
        # runs the suite.
        "security": {"secret_store": "file"},
    }
    (tmp_path / "vault").mkdir()
    return load_config(DEFAULTS_PATH, None, None, overrides)


class _CoreThread:
    """The core on its own event loop: sync Playwright cannot share a thread with asyncio."""

    def __init__(self, tmp_path: Path) -> None:
        self.core: NoxCore | None = None
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(tmp_path,), daemon=True)

    def _run(self, tmp_path: Path) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.core = NoxCore(_config(tmp_path), voice=False, profiles_dir=PROFILES_DIR)
            self.loop.run_until_complete(asyncio.wait_for(self.core.start(), START_TIMEOUT_S))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            self._error = exc
        finally:
            self._ready.set()
        if self._error is None:
            self.loop.run_forever()

    def start(self) -> NoxCore:
        self._thread.start()
        if not self._ready.wait(START_TIMEOUT_S + 5):
            raise TimeoutError("NoxCore did not start in time")
        if self._error is not None:
            raise self._error
        assert self.core is not None
        return self.core

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 30) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def submit(self, coro: Coroutine[Any, Any, T]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if self.core is not None:
            self.run(asyncio.wait_for(self.core.stop(), 30), timeout=35)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10)
        self.loop.close()


@pytest.fixture
def runner(tmp_path: Path) -> Iterator[_CoreThread]:
    reason = _ensure_ui_built()
    if reason:
        pytest.skip(reason)
    core_thread = _CoreThread(tmp_path)
    core = core_thread.start()
    runtime = core.extensions["lan"]
    assert isinstance(runtime, LanRuntime)
    deadline = time.monotonic() + 20
    while not (runtime.running or runtime.error) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runtime.running, runtime.error
    assert DASHBOARD_DIST.is_dir()
    try:
        yield core_thread
    finally:
        core_thread.stop()


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    folder = Path(os.environ.get("NOX_E2E_ARTIFACTS") or tmp_path / "artifacts") / "lan"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _axe(page: Page, where: str) -> None:
    """Fail on any serious or critical WCAG 2.2 A/AA violation on the page as it is now.

    axe is evaluated through the DevTools protocol, which the page's Content-Security-Policy does
    not govern - the page itself stays exactly as strict as it is for a person.
    """
    if not AXE_JS.is_file():
        pytest.fail(f"{AXE_JS} is missing - run `npm ci` in ui/dashboard")
    if not page.evaluate("() => typeof window.axe === 'object'"):
        page.evaluate(AXE_JS.read_text(encoding="utf-8") + "\n;undefined")
    violations = page.evaluate(
        """async (tags) => {
            const result = await window.axe.run(document, {
                runOnly: { type: 'tag', values: tags },
                resultTypes: ['violations'],
            });
            return result.violations
                .filter((v) => v.impact === 'serious' || v.impact === 'critical')
                .map((v) => ({
                    id: v.id,
                    impact: v.impact,
                    help: v.help,
                    nodes: v.nodes.slice(0, 5).map((n) => n.target.join(' ')),
                }));
        }""",
        AXE_TAGS,
    )
    assert not violations, f"accessibility violations on {where}: {violations}"


def _until(condition: Callable[[], bool], what: str, timeout_s: float = 15) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def _dismiss_first_run(page: Page, later: str) -> None:
    """A core without a name for its person opens the first-run flow; these tests skip it."""
    button = page.get_by_role("button", name=later)
    try:
        expect(button).to_be_visible(timeout=3000)
    except AssertionError:
        return
    button.click()
    expect(button).to_have_count(0)


def _open_area(page: Page, name: re.Pattern[str]) -> None:
    """The side bar on wide screens, the bottom bar on a phone: whichever is shown."""
    page.get_by_role("navigation").get_by_role("link", name=name).first.click()


def test_a_household_pairs_confirms_and_removes(
    runner: _CoreThread, browser: Browser, artifacts: Path
) -> None:
    core = runner.core
    assert core is not None and core.hub is not None and core.http is not None
    lan = core.extensions["lan"]
    assert isinstance(lan, LanRuntime)
    lan_base = f"https://127.0.0.1:{lan.port}/dashboard/?lang=de"

    contexts: list[BrowserContext] = []

    def window(name: str) -> Page:
        context = browser.new_context(
            **DEVICES[name],
            locale="de-DE",
            # Screenshots show where a step ends, not a frame of the fade into it.
            reduced_motion="reduce",
            ignore_https_errors=True,  # the browser warning a person clicks through once
            record_video_dir=str(artifacts / "video" / name),
            record_video_size=DEVICES[name]["viewport"],
        )
        contexts.append(context)
        return context.new_page()

    step = 0

    def shot(page: Page, name: str) -> None:
        nonlocal step
        step += 1
        page.screenshot(path=str(artifacts / f"{step:02d}-{name}.png"), full_page=False)

    try:
        # ---- 1. the PC makes a code; the phone pairs with it -----------------------------------
        token = read_session_token(Path(core.config.paths.runtime_dir))
        desktop = window("desktop")
        desktop.goto(f"{core.http.url}/dashboard/?ws={core.hub.port}&lang=de#token={token}")
        expect(desktop.locator(".app[data-conn=online]")).to_have_count(1, timeout=UI_TIMEOUT_MS)
        _dismiss_first_run(desktop, "Später einrichten")
        _open_area(desktop, re.compile("^Geräte"))
        expect(desktop.get_by_text("Noch kein Gerät gekoppelt.")).to_be_visible()
        # An unused Telegram is one quiet line, not a second big tile.
        expect(desktop.get_by_text("Handy über Telegram (aus)")).to_be_visible()
        shot(desktop, "desktop-devices-empty")
        _axe(desktop, "the PC's Devices tab")
        desktop.get_by_role("button", name="Handy koppeln").click()
        desktop.get_by_label("Name des Geräts (freiwillig)").fill("Handy")
        desktop.get_by_role("button", name="Code erzeugen").click()
        expect(desktop.get_by_role("img", name=re.compile("QR-Code zum Koppeln"))).to_be_visible()
        expect(desktop.get_by_text("Fingerabdruck des Zertifikats (SHA-256)")).to_be_visible()
        shot(desktop, "desktop-pairing-code")
        _axe(desktop, "the PC's pairing code")
        code = desktop.locator(".code-display-value").inner_text().replace(" ", "")
        assert re.fullmatch(r"[23456789A-HJKMNP-TV-Z]{8}", code), code

        phone = window("phone")
        phone.goto(f"{lan_base}#pair={code}")
        expect(phone.get_by_role("heading", name="Dieses Gerät mit Nox koppeln")).to_be_visible()
        assert "pair=" not in phone.url  # the code left the address bar at once
        name_field = phone.get_by_label("Name dieses Geräts (freiwillig)")
        expect(name_field).to_have_value("")
        expect(name_field).to_have_attribute("placeholder", "iPhone")
        shot(phone, "phone-pair-screen")
        _axe(phone, "the phone's pairing screen")
        phone.get_by_role("button", name="Koppeln").click()
        expect(phone.locator(".app[data-conn=online]")).to_have_count(1, timeout=UI_TIMEOUT_MS)
        shot(phone, "phone-dashboard")

        # ---- 2. the PC sees it arrive, under the name the PC gave it --------------------------
        expect(desktop.get_by_text("Handy ist gekoppelt.")).to_be_visible(timeout=UI_TIMEOUT_MS)
        # The pairing sheet closed by itself once the phone arrived.
        expect(desktop.get_by_label("Name des Geräts (freiwillig)")).to_have_count(0)
        devices = desktop.get_by_role("list", name="Gekoppelte Geräte")
        expect(devices.get_by_role("listitem").filter(has_text="Handy")).to_be_visible()
        shot(desktop, "desktop-device-arrived")
        _open_area(phone, re.compile("^Geräte"))
        expect(phone.get_by_text("dieses Gerät", exact=True)).to_be_visible()
        # No PIN is set: a phone cannot pair others, and says where that works.
        expect(
            phone.get_by_text("Neue Geräte koppelst du am Rechner, auf dem Nox läuft")
        ).to_be_visible()
        expect(
            phone.get_by_text("Fingerabdruck der Nox-Zertifizierungsstelle (SHA-256)")
        ).to_be_visible()
        shot(phone, "phone-devices")
        _axe(phone, "the phone's Devices tab")
        phone_id = next(d.id for d in lan.devices.devices() if d.name == "Handy")

        # ---- 3. high risk without a PIN: only the PC can allow --------------------------------
        engine = core.security.engine  # type: ignore[union-attr]

        async def ask(target: str, timeout: float) -> tuple[str, Decision]:
            request = PermissionRequest(
                agent="companion",
                tool="file",
                action="delete",
                mode="companion",
                risk=Risk.HIGH,
                target=target,
            )
            grant = engine.request_confirmation(request)
            result = await engine.await_confirmation(grant, timeout=timeout)
            return grant, result.decision

        def audit_by(request_id: str) -> str | None:
            store = core.security.audit_store  # type: ignore[union-attr]
            for entry in store.entries(limit=500):
                details = store.details(entry.seq)
                if details.get("request_id") == request_id and entry.result != "pending":
                    return str(details.get("by"))
            return None

        dialog_phone = phone.get_by_role("alertdialog", name="Darf Nox etwas löschen?")
        dialog_desktop = desktop.get_by_role("alertdialog", name="Darf Nox etwas löschen?")

        pending = runner.submit(ask("Archiv/alt.zip", 30))
        expect(dialog_phone).to_be_visible(timeout=UI_TIMEOUT_MS)
        expect(dialog_desktop).to_be_visible(timeout=UI_TIMEOUT_MS)
        expect(dialog_phone.get_by_text("Nox möchte löschen: Archiv/alt.zip")).to_be_visible()
        expect(dialog_phone.get_by_role("button", name="Erlauben", exact=True)).to_have_count(0)
        expect(dialog_phone.get_by_text("Erlauben geht dafür nur am Rechner")).to_be_visible()
        expect(dialog_phone.get_by_role("button", name=re.compile("Stunde"))).to_have_count(0)
        # High risk is asked every time (NEVER_REMEMBERED), so no prompt offers to remember it -
        # not even on the PC. The plain "Erlauben" is there.
        expect(dialog_desktop.get_by_role("button", name=re.compile("Stunde"))).to_have_count(0)
        expect(dialog_desktop.get_by_role("button", name="Erlauben", exact=True)).to_be_visible()
        expect(dialog_desktop.get_by_text(re.compile(r"Noch \d+ Sekunden"))).to_be_visible()
        assert "immer" not in dialog_desktop.inner_text().lower()
        shot(phone, "phone-permission-no-pin")
        shot(desktop, "desktop-permission-prompt")
        _axe(phone, "the phone's permission prompt")
        _axe(desktop, "the PC's permission prompt")
        dialog_desktop.get_by_role("button", name="Erlauben", exact=True).click()
        grant, decision = pending.result(15)
        assert decision is Decision.ALLOW
        expect(dialog_phone).to_have_count(0, timeout=UI_TIMEOUT_MS)
        expect(phone.get_by_text("Auf einem anderen Gerät entschieden.")).to_be_visible()
        _until(lambda: audit_by(grant) is not None, "the audit entry of the PC's answer")
        assert str(audit_by(grant)).startswith("dashboard:")

        # ---- 4. with a PIN set, the phone allows with it ---------------------------------------
        async def set_pin() -> None:
            core.security.pin.set_pin(PIN)  # type: ignore[union-attr]

        runner.run(set_pin())
        pending = runner.submit(ask("Archiv/neu.zip", 30))
        expect(dialog_phone).to_be_visible(timeout=UI_TIMEOUT_MS)
        pin_field = dialog_phone.get_by_label("Sicherheits-PIN")
        expect(pin_field).to_be_visible(timeout=UI_TIMEOUT_MS)
        pin_field.fill(PIN)
        shot(phone, "phone-permission-pin")
        dialog_phone.get_by_role("button", name="Erlauben", exact=True).click()
        grant, decision = pending.result(15)
        assert decision is Decision.ALLOW
        expect(dialog_desktop).to_have_count(0, timeout=UI_TIMEOUT_MS)
        expect(desktop.get_by_text("Auf einem anderen Gerät entschieden.")).to_be_visible()
        shot(desktop, "desktop-decided-elsewhere")
        _until(lambda: audit_by(grant) is not None, "the audit entry of the phone's answer")
        assert audit_by(grant) == f"device:{phone_id}"

        # ---- 5. denied on the PC; and one that nobody answers ---------------------------------
        pending = runner.submit(ask("Bilder/urlaub", 30))
        expect(dialog_desktop).to_be_visible(timeout=UI_TIMEOUT_MS)
        dialog_desktop.get_by_role("button", name="Ablehnen").click()
        grant, decision = pending.result(15)
        assert decision is Decision.DENY
        expect(dialog_phone).to_have_count(0, timeout=UI_TIMEOUT_MS)
        _until(lambda: audit_by(grant) is not None, "the audit entry of the PC's answer")
        assert str(audit_by(grant)).startswith("dashboard:")

        pending = runner.submit(ask("Musik/liste.txt", 2))
        expect(dialog_phone).to_be_visible(timeout=UI_TIMEOUT_MS)
        grant, decision = pending.result(15)
        assert decision is Decision.DENY  # unanswered means no
        expect(dialog_phone).to_have_count(0, timeout=UI_TIMEOUT_MS)
        expect(dialog_desktop).to_have_count(0, timeout=UI_TIMEOUT_MS)
        no_answer = "Keine Antwort – Nox hat es nicht getan."
        expect(phone.get_by_text(no_answer)).to_be_visible()
        expect(desktop.get_by_text(no_answer)).to_be_visible()
        shot(phone, "phone-no-answer")

        # ---- 6. the tablet, never paired, types a code the PC made with the PIN --------------
        tablet = window("tablet")
        tablet.goto(lan_base)
        expect(
            tablet.get_by_role("heading", name="Dieses Gerät ist nicht gekoppelt")
        ).to_be_visible()
        shot(tablet, "tablet-not-paired")
        _axe(tablet, "the tablet's not-paired screen")
        tablet.get_by_role("button", name="Ich habe einen Code").click()
        # The PC's page still believes no PIN is set: the refusal says one is needed, and the
        # field for it appears right there.
        desktop.get_by_role("button", name="Weiteres Gerät koppeln").click()
        desktop.get_by_role("button", name="Code erzeugen").click()
        expect(desktop.get_by_role("alert").filter(has_text="PIN")).to_be_visible()
        desktop.get_by_label("Sicherheits-PIN").fill(PIN)
        shot(desktop, "desktop-pair-with-pin")
        desktop.get_by_role("button", name="Code erzeugen").click()
        expect(desktop.locator(".code-display-value")).to_be_visible()
        second = desktop.locator(".code-display-value").inner_text()
        tablet.get_by_label("Code", exact=True).fill(second.lower())
        shot(tablet, "tablet-code-entry")
        tablet.get_by_role("button", name="Koppeln").click()
        expect(tablet.locator(".app[data-conn=online]")).to_have_count(1, timeout=UI_TIMEOUT_MS)
        shot(tablet, "tablet-dashboard")
        _open_area(tablet, re.compile("^Geräte"))
        expect(tablet.get_by_text("dieses Gerät", exact=True)).to_be_visible()
        shot(tablet, "tablet-devices")
        _axe(tablet, "the tablet's Devices tab")

        # ---- 7. the PC removes the phone - no PIN needed there --------------------------------
        row = devices.get_by_role("listitem").filter(has_text="Handy")
        row.get_by_role("button", name="Entfernen").click()
        expect(row.get_by_text("Zum Bestätigen noch einmal")).to_be_visible()
        armed = row.get_by_role("button", name="Wirklich entfernen")
        expect(armed).to_be_visible()
        shot(desktop, "desktop-remove-confirm")
        row.get_by_role("button", name="Wirklich entfernen").click()
        expect(phone.get_by_text("Dieses Gerät wurde entfernt.", exact=False)).to_be_visible(
            timeout=UI_TIMEOUT_MS
        )
        shot(phone, "phone-removed")
        assert all(d.revoked for d in lan.devices.devices() if d.id == phone_id)
    finally:
        for context in contexts:
            context.close()  # finishes the videos
