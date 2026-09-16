"""System tray icon and menu (D35, D235, D239): capture-tinted icon, privacy, mute, dashboard."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from nox.core.state import PrivacyMode
from nox.shell.logic import TRAY_COLOURS, ShellModel, TrayTint, tray_tint, tray_tooltip


def make_tray_icon(tint: TrayTint, size: int = 32) -> QIcon:
    """Filled circle in the tint colour; inner glyph differs per tint, not colour-only (D238)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    colour = QColor(TRAY_COLOURS[tint])
    painter.setBrush(QBrush(colour))
    painter.setPen(QPen(QColor("#0b0b12"), 2))
    margin = size // 6
    painter.drawEllipse(QRect(margin, margin, size - 2 * margin, size - 2 * margin))
    painter.setBrush(QBrush(QColor("#0b0b12")))
    painter.setPen(Qt.PenStyle.NoPen)
    if tint in (TrayTint.CAPTURING, TrayTint.CLOUD):
        painter.drawEllipse(QRect(size // 2 - 4, size // 2 - 4, 8, 8))  # dot = capture running
    elif tint == TrayTint.SAFE_MODE:
        painter.drawRect(QRect(size // 2 - 5, size // 2 - 5, 10, 10))  # square = stopped
    elif tint == TrayTint.OFFLINE:
        painter.drawRect(QRect(size // 2 - 7, size // 2 - 2, 14, 4))  # bar = no connection
    elif tint == TrayTint.MUTED:
        painter.drawRect(QRect(size // 2 - 2, size // 2 - 7, 4, 14))  # vertical bar = muted
    painter.end()
    return QIcon(pix)


class TrayController:
    """Owns the QSystemTrayIcon; all actions call back into the ShellApp."""

    def __init__(
        self,
        *,
        on_toggle_pet: Callable[[], None],
        on_privacy: Callable[[PrivacyMode], None],
        on_mute: Callable[[], None],
        on_dashboard: Callable[[], None],
        on_kill: Callable[[], None],
        on_quit: Callable[[], None],
        on_click_through: Callable[[bool], None],
        language: str = "de",
    ) -> None:
        self._language = language
        de = language.startswith("de")
        self.tray = QSystemTrayIcon()
        self.menu = QMenu()
        self._tint: TrayTint | None = None

        self.action_toggle_pet = QAction("Pet anzeigen/verbergen" if de else "Show/hide pet")
        self.action_toggle_pet.triggered.connect(on_toggle_pet)
        self.menu.addAction(self.action_toggle_pet)

        self.action_click_through = QAction("Klick-durchlässig" if de else "Click-through")
        self.action_click_through.setCheckable(True)
        self.action_click_through.toggled.connect(on_click_through)
        self.menu.addAction(self.action_click_through)

        privacy_menu = self.menu.addMenu("Privacy")
        self._privacy_group = QActionGroup(self.menu)
        self._privacy_group.setExclusive(True)
        self.privacy_actions: dict[PrivacyMode, QAction] = {}
        for mode in PrivacyMode:
            act = QAction(mode.value.capitalize())
            act.setCheckable(True)
            act.setData(mode.value)
            act.triggered.connect(lambda _checked=False, m=mode: on_privacy(m))
            self._privacy_group.addAction(act)
            privacy_menu.addAction(act)
            self.privacy_actions[mode] = act

        self.action_mute = QAction("Stumm" if de else "Mute")
        self.action_mute.setCheckable(True)
        self.action_mute.triggered.connect(lambda _checked=False: on_mute())
        self.menu.addAction(self.action_mute)

        self.menu.addSeparator()
        self.action_dashboard = QAction("Dashboard öffnen" if de else "Open dashboard")
        self.action_dashboard.triggered.connect(on_dashboard)
        self.menu.addAction(self.action_dashboard)

        self.menu.addSeparator()
        self.action_kill = QAction("Notaus (Kill switch)" if de else "Kill switch")
        self.action_kill.triggered.connect(on_kill)
        self.menu.addAction(self.action_kill)

        self.action_quit = QAction("Beenden" if de else "Quit")
        self.action_quit.triggered.connect(on_quit)  # ShellApp.quit(): sup.stop, B-6, not a kill
        self.menu.addAction(self.action_quit)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self._on_toggle_pet = on_toggle_pet

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._on_toggle_pet()

    def show(self) -> None:
        self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    def refresh(self, model: ShellModel) -> TrayTint:
        """Re-tint icon, tooltip and checked states from the model. Returns the tint applied."""
        tint = tray_tint(model)
        if tint != self._tint:
            self.tray.setIcon(make_tray_icon(tint))
            self._tint = tint
        self.tray.setToolTip(tray_tooltip(model, self._language))
        for mode, act in self.privacy_actions.items():
            act.blockSignals(True)
            act.setChecked(mode == model.privacy_mode)
            act.setEnabled(model.connected)
            act.blockSignals(False)
        self.action_mute.blockSignals(True)
        self.action_mute.setChecked(model.muted)
        self.action_mute.setEnabled(model.connected)
        self.action_mute.blockSignals(False)
        self.action_click_through.blockSignals(True)
        self.action_click_through.setChecked(model.click_through)
        self.action_click_through.blockSignals(False)
        self.action_dashboard.setEnabled(model.connected)
        return tint

    def notify(self, title: str, text: str, critical: bool = False) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Critical
            if critical
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.tray.showMessage(title, text, icon, 4000)
