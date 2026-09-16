"""Frameless transparent always-on-top pet window hosting the web renderer (ADR-004, SP-09, B-7).

Validated in SP-09: WA_TranslucentBackground + page background transparent renders without a black
backing on Windows 11; click-through via WS_EX_TRANSPARENT. Dragging is done by an event filter on
the view's render widget so a plain click still reaches the page (which sends `pet.interact`).

B-7 (shell RSS reduction, ADR-004 "Measured" section): the pet view runs on a single
process-lifetime off-the-record `QWebEngineProfile` (see `_pet_profile`) instead of Qt's default
profile. Off-the-record means no cache, cookies, local storage, or service-worker/CacheStorage
registration ever touches disk, which matters here specifically because the page holds the IPC
session token in memory (never in `localStorage`, IPC Model) — this profile makes persisting it
impossible even by accident. `PersistentCookiesPolicy.NoPersistentCookies` states that intent
explicitly (an off-the-record profile already implies it); spellcheck, browser plugins and the
`getDisplayMedia` screen-capture API are disabled since a kiosk-style pet window needs none of them.

GPU: hardware compositing stays on by default. Set the environment variable
`NOX_PET_SOFTWARE_RENDER=1` before launching the shell to force Chromium's software compositor
(`QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu-compositing`) instead — e.g. as a fallback on a machine
where GPU compositing under a transparent, click-through, always-on-top window misbehaves. Qt reads
`QTWEBENGINE_CHROMIUM_FLAGS` when the `QApplication` is constructed, so `nox.shell.app.run()` sets
it before that call; this module only documents and consumes the resulting behaviour.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QUrl
from PySide6.QtGui import QMouseEvent
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget

from nox.shell import win32
from nox.shell.logutil import get_logger
from nox.shell.runtime import ShellState

log = get_logger(__name__)

DRAG_THRESHOLD_PX = 6
PET_FLAGS = (
    Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
)

_pet_profile_instance: QWebEngineProfile | None = None


def _pet_profile() -> QWebEngineProfile:
    """One off-the-record `QWebEngineProfile` shared by every pet view in this process (B-7)."""
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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_moved = on_moved
        self._press_global: QPoint | None = None
        self._press_window: QPoint | None = None
        self._dragging = False
        self._filtered: QObject | None = None
        self._click_through = False
        self._url: str | None = None

        self.setWindowFlags(PET_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("Nox")
        self.resize(state.width, state.height)
        if state.x is not None and state.y is not None:
            self.move(state.x, state.y)

        self.view = QWebEngineView(self)
        self.view.setPage(QWebEnginePage(_pet_profile(), self.view))
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        self.view.setGeometry(0, 0, state.width, state.height)
        self.view.loadFinished.connect(self._install_filter)

    # -- loading ---------------------------------------------------------------------------------

    def load(self, url: str) -> None:
        """Load the pet page. The token is in the URL fragment; never log the URL."""
        self._url = url
        self.view.load(QUrl(url))
        log.info("pet_window.load", host=QUrl(url).host())

    def show_offline_page(self) -> None:
        """Honest offline page when the core HTTP server is not there (Process Model, shell)."""
        self.view.setHtml(OFFLINE_HTML, QUrl("about:offline"))

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        self.view.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)  # type: ignore[arg-type]

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
                if was_drag and self._on_moved is not None:
                    self._on_moved(self.x(), self.y())
        return False  # never swallow: the page still gets clicks for pet.interact


OFFLINE_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:transparent;font-family:Segoe UI,system-ui,sans-serif;color:#c8c8d4}
.wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap:10px}
.orb{width:110px;height:96px;border-radius:50%;background:#3a3a46;border:2px solid #55555f;
 position:relative}
.orb:before,.orb:after{content:"";position:absolute;top:36px;width:22px;height:4px;background:#0b0b12;border-radius:2px}
.orb:before{left:24px}.orb:after{right:24px}
.label{font-size:12px;background:rgba(11,11,18,.85);padding:4px 10px;border-radius:10px}
</style></head><body><div class="wrap" role="status" aria-label="Nox offline">
<div class="orb"></div><div class="label">Nox: Kern nicht erreichbar / core offline</div>
</div></body></html>"""
