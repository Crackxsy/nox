"""Frameless transparent always-on-top pet window hosting the web renderer.

Validated in: WA_TranslucentBackground + page background transparent renders without a black
backing on Windows 11; click-through via WS_EX_TRANSPARENT. Dragging is done by an event filter on
the view's render widget so a plain click still reaches the page (which sends `pet.interact`).
(shell RSS reduction, "Measured" section): the pet view runs on a single process-lifetime off-the-
record `QWebEngineProfile` (see `_pet_profile`) instead of Qt's default profile. Off-the-record
means no cache, cookies, local storage, or service-worker/CacheStorage registration ever touches
disk, which matters here specifically because the page holds the IPC session token in memory (never
in `localStorage`, IPC Model) — this profile makes persisting it impossible even by accident.
`PersistentCookiesPolicy.NoPersistentCookies` states that intent explicitly (an off-the-record
profile already implies it); spellcheck, browser plugins and the `getDisplayMedia` screen-capture
API are disabled since a kiosk-style pet window needs none of them.

GPU: hardware compositing stays on by default. Set the environment variable
`NOX_PET_SOFTWARE_RENDER=1` before launching the shell to force Chromium's software compositor
(`QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu-compositing`) instead — e.g. as a fallback on a machine
where GPU compositing under a transparent, click-through, always-on-top window misbehaves. Qt reads
`QTWEBENGINE_CHROMIUM_FLAGS` when the `QApplication` is constructed, so `nox.shell.app.run` sets it
before that call; this module only documents and consumes the resulting behaviour.

Placement and quiet: the window opens on `pet.default_monitor` unless its saved position is still
on a screen, is pulled back whenever it ends up off the screens that exist (a monitor unplugged, a
smaller resolution, a drag too far), and can be told that a game is running so the page draws at
`pet.fps_in_game` instead of `pet.fps_target`. The rules themselves are plain functions in
`nox.shell.pet_layout` and `nox.shell.pet_quiet`; this class only applies their answers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QGuiApplication, QMouseEvent, QScreen
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget

from nox.shell import win32
from nox.shell.logutil import get_logger
from nox.shell.pet_layout import Monitor, Rect, corrected_rect, initial_rect, without_monitor
from nox.shell.runtime import ShellState

log = get_logger(__name__)

DRAG_THRESHOLD_PX = 6
PET_FLAGS = (
    Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
)
#: The page reads its frame rate from this global and re-reads it on the `nox-pet-frames` event.
FRAMES_GLOBAL = "__noxPetFrames"
FRAMES_EVENT = "nox-pet-frames"
#: After a screen is removed, the screens are looked at once more: the list Qt hands out can still
#: be settling when the signal arrives.
SCREEN_RECHECK_MS = 500

_pet_profile_instance: QWebEngineProfile | None = None


def qt_monitors() -> list[Monitor]:
    """The screens Qt knows, as plain monitors: their usable area (without the task bar)."""
    primary = QGuiApplication.primaryScreen()
    monitors: list[Monitor] = []
    for screen in QGuiApplication.screens():
        area = screen.availableGeometry()
        monitors.append(
            Monitor(Rect(area.x(), area.y(), area.width(), area.height()), screen == primary)
        )
    return monitors


def _pet_profile() -> QWebEngineProfile:
    """One off-the-record `QWebEngineProfile` shared by every pet view in this process."""
    global _pet_profile_instance
    if _pet_profile_instance is None:
        profile = QWebEngineProfile()  # no storageName argument -> off-the-record, no disk state
        profile.setSpellCheckEnabled(False)
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
        )
        settings = profile.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ScreenCaptureEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        _pet_profile_instance = profile
    return _pet_profile_instance


class PetWindow(QWidget):
    def __init__(
        self,
        state: ShellState,
        *,
        on_moved: Callable[[int, int], None] | None = None,
        default_monitor: str = "left",
        monitors: Callable[[], list[Monitor]] = qt_monitors,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_moved = on_moved
        self._default_monitor = default_monitor
        self._monitors = monitors
        self._frames: dict[str, Any] | None = None
        self._press_global: QPoint | None = None
        self._press_window: QPoint | None = None
        self._dragging = False
        self._filtered: QObject | None = None
        self._click_through = False
        self._url: str | None = None

        self.setWindowFlags(PET_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Coming back after a full-screen game must not take the keyboard away from whatever the
        # user went back to.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Nox")
        self.resize(state.width, state.height)
        saved = (state.x, state.y) if state.x is not None and state.y is not None else None
        placed = initial_rect(saved, (state.width, state.height), default_monitor, monitors())
        if placed is not None:
            self.move(placed.x, placed.y)

        self.view = QWebEngineView(self)
        self.view.setPage(QWebEnginePage(_pet_profile(), self.view))
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        self.view.setGeometry(0, 0, state.width, state.height)
        self.view.loadFinished.connect(self._on_load_finished)
        self._watch_screens()

    # -- loading ---------------------------------------------------------------------------------

    def load(self, url: str) -> None:
        """Load the pet page. The token is in the URL fragment; never log the URL."""
        self._url = url
        self.view.load(QUrl(url))
        log.info("pet_window.load", host=QUrl(url).host())

    def show_offline_page(self) -> None:
        """Honest offline page when the core HTTP server is not there (Process Model, shell)."""
        self.view.setHtml(OFFLINE_HTML, QUrl("about:offline"))

    def _on_load_finished(self, ok: bool) -> None:
        self._install_filter(ok)
        if ok:
            self._push_frames()  # a reloaded page starts from its defaults again

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        self.view.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)  # type: ignore[arg-type]

    # -- placement -------------------------------------------------------------------------------

    def keep_visible(self, *, notify: bool = True, leaving: Rect | None = None) -> bool:
        """Move the window back onto the screens that exist, if it is not on them. Returns whether
        it moved. Called when a screen comes, goes or changes, and after a drag. `leaving` is the
        usable area of a screen that is being removed and must not count any more."""
        current = Rect(self.x(), self.y(), self.width(), self.height())
        monitors = self._monitors()
        if leaving is not None:
            monitors = without_monitor(monitors, leaving)
        fixed = corrected_rect(current, self._default_monitor, monitors)
        if fixed == current:
            return False
        self.move(fixed.x, fixed.y)
        log.info("pet_window.moved_back_on_screen")
        if notify and self._on_moved is not None:
            self._on_moved(self.x(), self.y())
        return True

    def set_default_monitor(self, setting: str) -> None:
        """`pet.default_monitor` changed: it applies the next time the window needs a place."""
        self._default_monitor = setting

    def _watch_screens(self) -> None:
        app = QGuiApplication.instance()
        if not isinstance(app, QGuiApplication):
            return
        for screen in QGuiApplication.screens():
            self._watch(screen)
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screen_removed)

    def _watch(self, screen: QScreen) -> None:
        screen.availableGeometryChanged.connect(self._on_screens_changed)

    def _on_screen_added(self, screen: QScreen) -> None:
        self._watch(screen)
        self.keep_visible()

    def _on_screen_removed(self, screen: QScreen) -> None:
        area = screen.availableGeometry()
        self.keep_visible(leaving=Rect(area.x(), area.y(), area.width(), area.height()))
        QTimer.singleShot(SCREEN_RECHECK_MS, self.keep_visible)

    def _on_screens_changed(self, *_args: object) -> None:
        self.keep_visible()

    # -- quiet -----------------------------------------------------------------------------------

    def frames_script(self) -> str | None:
        """The script that tells the page its frame budget, or None before it was set."""
        if self._frames is None:
            return None
        return (
            f"window.{FRAMES_GLOBAL}={json.dumps(self._frames)};"
            f"window.dispatchEvent(new CustomEvent({json.dumps(FRAMES_EVENT)}));"
        )

    def set_frames(self, message: dict[str, Any]) -> None:
        """Tell the page how fast to draw (`nox.shell.pet_quiet.frames_message`). Remembered, and
        sent again after every page load."""
        if message == self._frames:
            return
        self._frames = message
        self._push_frames()

    def _push_frames(self) -> None:
        script = self.frames_script()
        if script is not None:
            self.view.page().runJavaScript(script)

    # -- click-through ---------------------------------------------------------------------------

    @property
    def click_through(self) -> bool:
        return self._click_through

    def set_click_through(self, enabled: bool) -> bool:
        """Toggle WS_EX_TRANSPARENT. Returns the effective state (False if unsupported)."""
        applied = win32.set_click_through(int(self.winId()), enabled)
        self._click_through = enabled if applied else False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self._click_through)
        return self._click_through

    # -- drag ------------------------------------------------------------------------------------

    def _install_filter(self, ok: bool) -> None:
        proxy = self.view.focusProxy()
        if proxy is not None and proxy is not self._filtered:
            if self._filtered is not None:
                self._filtered.removeEventFilter(self)
            proxy.installEventFilter(self)
            self._filtered = proxy

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if isinstance(event, QMouseEvent):
            is_left = event.button() == Qt.MouseButton.LeftButton
            if event.type() == QEvent.Type.MouseButtonPress and is_left:
                self._press_global = event.globalPosition().toPoint()
                self._press_window = self.pos()
                self._dragging = False
            elif event.type() == QEvent.Type.MouseMove and self._press_global is not None:
                delta = event.globalPosition().toPoint() - self._press_global
                if not self._dragging and delta.manhattanLength() >= DRAG_THRESHOLD_PX:
                    self._dragging = True
                if self._dragging and self._press_window is not None:
                    self.move(self._press_window + delta)
            elif event.type() == QEvent.Type.MouseButtonRelease and self._press_global is not None:
                was_drag = self._dragging
                self._press_global = None
                self._press_window = None
                self._dragging = False
                if was_drag:
                    self.keep_visible(notify=False)  # a drag too far comes back, then is saved
                    if self._on_moved is not None:
                        self._on_moved(self.x(), self.y())
        return False  # never swallow: the page still gets clicks for pet.interact


#: The page the pet window shows when the web bundle cannot be reached at all — the one screen the
#: shell can draw without the core. It carries the values of `ui/shared/tokens.css` inline (they
#: cannot be imported here: this string is loaded from `about:offline`, which has no origin to
#: resolve a stylesheet against), both schemes, so the window does not glow dark on a light desktop.
#: German only, one sentence, and one thing to do — the old version was dark-only, bilingual
#: ("Nox: Kern nicht erreichbar / core offline") and offered no action at all.
OFFLINE_HTML = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="color-scheme" content="light dark"><style>
:root{
  --ink:#1d1d1f; --ink-2:#6e6e73; --chip:rgba(255,255,255,.86); --line:#86868b;
  --body:#8e8e93; --eye:#1d1d1f;
  --font:-apple-system,"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
}
@media (prefers-color-scheme: dark){
  :root{ --ink:#f5f5f7; --ink-2:#98989d; --chip:rgba(0,0,0,.84); --line:#646469;
         --body:#6a6a70; --eye:#f5f5f7; }
}
html,body{margin:0;background:transparent;font-family:var(--font);color:var(--ink)}
.wrap{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
 height:100vh;gap:10px;padding-bottom:10px;box-sizing:border-box}
.orb{width:104px;height:92px;border-radius:50%;background:var(--body);opacity:.55;
 position:relative;flex:0 0 auto;margin-bottom:auto;margin-top:18%}
.orb:before,.orb:after{content:"";position:absolute;top:34px;width:20px;height:4px;
 background:var(--eye);border-radius:2px;opacity:.8}
.orb:before{left:22px}.orb:after{right:22px}
.chip{background:var(--chip);border:1px solid var(--line);border-radius:10px;
 padding:5px 10px;text-align:center;max-width:94%}
.title{font-size:12px;line-height:16px}
.hint{font-size:10px;line-height:14px;color:var(--ink-2)}
</style></head>
<body><div class="wrap" role="status">
<div class="orb" aria-hidden="true"></div>
<div class="chip">
<div class="title">Nox ist gerade nicht erreichbar.</div>
<div class="hint">Über das Nox-Symbol im Infobereich starten.</div>
</div>
</div></body></html>"""
