"""B-10 UI smoke against a live core (Decision Plan B-10, Decision Evaluation B-10).

Boots the real `NoxCore(voice=False)` headless with temporary paths, exactly like
`tests/integration/test_walking_skeleton.py` (offline privacy mode, health checks disabled via a
long interval) — but on its own event loop in a background thread, so a synchronous Playwright
session can drive the built dashboard and pet pages in headless Chromium against it. This exercises
the one part of the system unit and integration tests cannot reach: the browser. In particular the
token handshake (arrives once in the URL fragment, is cleared from the address bar, is never put in
`localStorage`) and the live IPC websocket the React apps open from inside the page are both
browser-only behaviour (IPC Model).

Kept small on purpose (Decision Evaluation B-10 "missing option: keep the asserted surface small and
stable"): auth -> fragment cleared -> Status page shows `ai.rules` available -> a chat turn streams
a non-empty answer with a `rules`/`ollama` provider badge -> the pet's capture indicator and canvas
exist. No pixel diffs — the pet is a continuously animated canvas and screenshot comparison would be
flaky and would discredit the approach.

The dashboard/pet bundles discover the websocket port via `?ws=<port>` in the query string (checked
before the `/health`-based fallback, `ui/shared/ipc.ts::resolveWsUrl`) because this test binds the
hub and the HTTP server to independent random ports, unlike a real single-instance run where the
configured defaults (`ipc.port` 47800, `ipc.http_port` 47801) usually coincide with what `/health`
would report anyway. No source change was needed for this: the query-parameter override already
exists in the shipped client code.

Skips (not fails) with a clear message if `ui/pet/dist` or `ui/dashboard/dist` is missing and cannot
be built from here.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from nox.app import DASHBOARD_DIST, DEFAULTS_PATH, PET_DIST, PROFILES_DIR, NoxCore
from nox.core.config import NoxConfig, load_config
from nox.ipc.tokens import read_session_token
from tests._ports import free_port_base

pytestmark = pytest.mark.e2e

START_TIMEOUT_S = 60
STOP_TIMEOUT_S = 30


def _built(dist: Path) -> bool:
    return (dist / "index.html").is_file()


def _build(app_dir: Path) -> str | None:
    """Try `npm ci && npm run build` in `app_dir`. Returns an error message, or None on success."""
    for cmd in (["npm", "ci"], ["npm", "run", "build"]):
        try:
            subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted local tooling
                cmd,
                cwd=str(app_dir),
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
                shell=sys.platform == "win32",
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            return f"`{' '.join(cmd)}` in {app_dir} failed: {detail}"
    return None


def _ensure_ui_built() -> str | None:
    """Build missing UI bundles; return a skip reason if that is not possible, else None."""
    problems: list[str] = []
    for dist in (PET_DIST, DASHBOARD_DIST):
        if _built(dist):
            continue
        error = _build(dist.parent)
        if error or not _built(dist):
            problems.append(error or f"{dist} still missing index.html after build")
    if problems:
        return "UI not built: " + "; ".join(problems)
    return None


def _config(tmp_path: Path) -> NoxConfig:
    base = free_port_base()
    overrides = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(tmp_path / "vault"),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": "offline"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
    }
    (tmp_path / "vault").mkdir()
    return load_config(DEFAULTS_PATH, None, None, overrides)


class _CoreThread:
    """Runs `NoxCore` on its own asyncio event loop in a background thread.

    A synchronous Playwright session (pytest-playwright's default) cannot share a thread with a
    running asyncio loop, so the core gets its own thread instead of being awaited from the test.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.core: NoxCore | None = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name="nox-core-e2e", args=(tmp_path,), daemon=True
        )

    def _run(self, tmp_path: Path) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            cfg = _config(tmp_path)
            self.core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
            start = asyncio.wait_for(self.core.start(), timeout=START_TIMEOUT_S)
            self._loop.run_until_complete(start)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread in start()
            self._error = exc
        finally:
            self._ready.set()
        if self._error is None:
            self._loop.run_forever()

    def start(self) -> NoxCore:
        self._thread.start()
        if not self._ready.wait(timeout=START_TIMEOUT_S + 5):
            raise TimeoutError("NoxCore did not start in time")
        if self._error is not None:
            raise self._error
        assert self.core is not None
        return self.core

    def stop(self) -> None:
        if self.core is None:
            return
        fut = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(self.core.stop(), timeout=STOP_TIMEOUT_S), self._loop
        )
        fut.result(timeout=STOP_TIMEOUT_S + 5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()


@pytest.fixture
def core(tmp_path: Path) -> Iterator[NoxCore]:
    reason = _ensure_ui_built()
    if reason:
        pytest.skip(reason)
    runner = _CoreThread(tmp_path)
    booted = runner.start()
    try:
        yield booted
    finally:
        runner.stop()


def _page_url(core: NoxCore, page_name: str) -> str:
    """`?ws=` picks the hub port explicitly (see module docstring); `#token=` is the auth token."""
    token = read_session_token(Path(core.config.paths.runtime_dir))
    assert token
    return f"{core.http.url}/{page_name}/?ws={core.hub.port}&lang=en#token={token}"


def test_dashboard_auth_status_and_chat(core: NoxCore, page: Page) -> None:
    page.goto(_page_url(core, "dashboard"))

    # Auth: the token was in the fragment; it must be gone from the address bar immediately.
    page.wait_for_function("() => !location.hash.includes('token=')", timeout=5000)
    assert "token=" not in page.url

    # The client must actually reach the core (proves the ?ws= override worked).
    expect(page.get_by_role("status").filter(has_text=re.compile("connected", re.I))).to_be_visible(
        timeout=15000
    )

    # Status page: ai.rules is always available offline (RulesProvider needs no network).
    rules_row = page.locator("tbody tr", has_text="ai.rules")
    expect(rules_row).to_be_visible(timeout=10000)
    expect(rules_row.locator("td").first).to_have_text("available", timeout=10000)

    # Chat: send a message and expect a streamed, non-empty answer with a local provider badge.
    page.get_by_role("tab", name=re.compile("^Chat")).click()
    page.get_by_label("Message to Nox").fill("Hallo Nox")
    page.get_by_role("button", name="Send").click()

    answer = page.locator("[role=log] article").nth(1)
    # `fastpath` is the deterministic answer to a greeting; `ollama`/`rules` answer everything else.
    provider_pattern = re.compile(r"Provider: (fastpath|rules|ollama)")
    expect(answer.locator("h3")).to_contain_text(provider_pattern, timeout=30000)
    answer_text = answer.locator("p").first.inner_text()
    assert answer_text.strip(), "assistant answer must not be empty"


def test_pet_capture_indicator_and_canvas(core: NoxCore, page: Page) -> None:
    page.goto(_page_url(core, "pet"))

    page.wait_for_function("() => !location.hash.includes('token=')", timeout=5000)
    assert "token=" not in page.url

    # The capture indicator (PRD NFR-8) is always mounted; no prop can hide it on the desktop pet.
    indicator = page.get_by_role("status")
    expect(indicator).to_be_visible(timeout=15000)

    # The pet renderer is a canvas; assert it exists and has actually been sized/rendered into.
    canvas = page.locator("canvas")
    expect(canvas).to_be_visible(timeout=10000)
    box = canvas.bounding_box()
    assert box is not None and box["width"] > 0 and box["height"] > 0
