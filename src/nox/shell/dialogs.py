"""Native confirmation dialog for `security.permission_requested` (Process Model, shell duties).

The dialog only collects a decision; the core enforces it. Voice never confirms here.

It says what the dashboard's permission prompt says (`ui/dashboard/src/PermissionPrompt.tsx`), in
the same words: the title is the yes/no question ("Darf Nox etwas löschen?"), then one sentence
with the device on its own ("Nox möchte auf **Laptop** löschen: Archiv/neu.zip"), the risk as a
word and the time left ("Risiko hoch · Noch 60 Sekunden. Ohne Antwort tut Nox es nicht."), a
countdown, "Für 8 Stunden erlauben" where the core keeps such a grant, and the security PIN only
while Nox controls a paired PC. Identifiers go under the details ("Technische Angaben").
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QLineEdit, QMessageBox, QWidget

from nox.shell.logic import (
    permission_device,
    permission_reply,
    rememberable,
    voice_satellite_line,
)

#: The dashboard's question per action (`QUESTION_KEYS`); anything else asks "Darf Nox das?".
_QUESTIONS = {
    "de": {
        "process_start": "Darf Nox ein Programm starten?",
        "process_stop": "Darf Nox ein Programm beenden?",
        "delete": "Darf Nox etwas löschen?",
        "write": "Darf Nox etwas speichern?",
        "move": "Darf Nox etwas verschieben?",
        "read": "Darf Nox etwas lesen?",
        "type_text": "Darf Nox Maus und Tastatur benutzen?",
        "input_send": "Darf Nox Maus und Tastatur benutzen?",
        "screen_capture": "Darf Nox den Bildschirm ansehen?",
        "software_install": "Darf Nox etwas installieren?",
        "settings": "Darf Nox eine Einstellung ändern?",
        "set": "Darf Nox eine Einstellung ändern?",
        "request": "Darf Nox ins Internet?",
    },
    "en": {
        "process_start": "May Nox start a program?",
        "process_stop": "May Nox close a program?",
        "delete": "May Nox delete something?",
        "write": "May Nox save something?",
        "move": "May Nox move something?",
        "read": "May Nox read something?",
        "type_text": "May Nox use mouse and keyboard?",
        "input_send": "May Nox use mouse and keyboard?",
        "screen_capture": "May Nox look at the screen?",
        "software_install": "May Nox install something?",
        "settings": "May Nox change a setting?",
        "set": "May Nox change a setting?",
        "request": "May Nox go online?",
    },
}

#: The dashboard's action words (`audit_action_*`); an unknown action keeps its identifier.
_ACTIONS = {
    "de": {
        "read": "lesen",
        "write": "schreiben",
        "delete": "löschen",
        "move": "verschieben",
        "list": "auflisten",
        "process_start": "Programm starten",
        "process_stop": "Programm beenden",
        "type_text": "Text eintippen",
        "input_send": "Tasten oder Maus senden",
        "screen_capture": "Bildschirm aufnehmen",
        "software_install": "Software installieren",
        "settings": "Einstellungen ändern",
        "set": "setzen",
        "start": "starten",
    },
    "en": {
        "read": "read",
        "write": "write",
        "delete": "delete",
        "move": "move",
        "list": "list",
        "process_start": "start a program",
        "process_stop": "stop a program",
        "type_text": "type text",
        "input_send": "send keys or mouse",
        "screen_capture": "capture the screen",
        "software_install": "install software",
        "settings": "change settings",
        "set": "set",
        "start": "start",
    },
}

_RISKS = {
    "de": {
        "read": "nur lesen",
        "low": "gering",
        "medium": "mittel",
        "high": "hoch",
        "critical": "kritisch",
    },
    "en": {
        "read": "read only",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "critical": "critical",
    },
}

_TEXT = {
    "de": {
        "title": "Darf Nox das?",
        "sentence": "Nox möchte {action}: {target}",
        "sentence_none": "Nox möchte {action}.",
        "sentence_device": "Nox möchte auf {device} {action}: {target}",
        "sentence_device_none": "Nox möchte auf {device} {action}.",
        "risk": "Risiko {risk}",
        "left": "Noch {n} Sekunden. Ohne Antwort tut Nox es nicht.",
        "over": "Die Zeit ist um – ohne Antwort tut Nox es nicht.",
        "remember_hours": "Für {n} Stunden erlauben",
        "remember_hour": "Für 1 Stunde erlauben",
        "remember_minutes": "Für {n} Minuten erlauben",
        "remember_minute": "Für 1 Minute erlauben",
        "allow": "Erlauben",
        "deny": "Ablehnen",
        "pin": "Sicherheits-PIN",
        "pin_hint": (
            "Nox steuert gerade Maus und Tastatur auf einem PC: Erlauben braucht jetzt die PIN."
        ),
        "agent": "Wer fragt",
        "tool": "Werkzeug",
        "action": "Aktion",
        "target": "Ziel",
        "device": "Gerät",
        "mode": "Modus",
    },
    "en": {
        "title": "May Nox do this?",
        "sentence": "Nox wants to {action}: {target}",
        "sentence_none": "Nox wants to {action}.",
        "sentence_device": "On {device}, Nox wants to {action}: {target}",
        "sentence_device_none": "On {device}, Nox wants to {action}.",
        "risk": "Risk {risk}",
        "left": "{n} seconds left. Without an answer, Nox will not do it.",
        "over": "Time is up – without an answer, Nox will not do it.",
        "remember_hours": "Allow for {n} hours",
        "remember_hour": "Allow for 1 hour",
        "remember_minutes": "Allow for {n} minutes",
        "remember_minute": "Allow for 1 minute",
        "allow": "Allow",
        "deny": "Deny",
        "pin": "Security PIN",
        "pin_hint": "Nox is controlling mouse and keyboard on a PC: allowing now needs the PIN.",
        "agent": "Asked by",
        "tool": "Tool",
        "action": "Action",
        "target": "Target",
        "device": "Device",
        "mode": "Mode",
    },
}


def _lang(language: str) -> str:
    return "de" if language.startswith("de") else "en"


def question(request: dict[str, Any], language: str = "de") -> str:
    """The dialog's title: the dashboard's yes/no question for this action."""
    lang = _lang(language)
    return _QUESTIONS[lang].get(str(request.get("action", "")), _TEXT[lang]["title"])


def sentence(request: dict[str, Any], language: str = "de", *, rich: bool = False) -> str:
    """'Nox möchte auf Laptop löschen: Archiv/neu.zip' - the device in bold when `rich` (then
    everything else is escaped: names and targets are never markup)."""
    lang = _lang(language)
    t = _TEXT[lang]
    action_id = str(request.get("action", ""))
    action = _ACTIONS[lang].get(action_id, action_id or "?")
    device, target = permission_device(request)

    def esc(text: str) -> str:
        return html.escape(text) if rich else text

    if device:
        named = f"<b>{esc(device)}</b>" if rich else device
        key = "sentence_device" if target else "sentence_device_none"
        return t[key].format(device=named, action=esc(action), target=esc(target))
    key = "sentence" if target else "sentence_none"
    return t[key].format(action=esc(action), target=esc(target))


def seconds_left(request: dict[str, Any], now: datetime | None = None) -> int | None:
    """Whole seconds until the core gives up on this question; None when it did not say."""
    raw = request.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    left = (expires - (now or datetime.now(UTC))).total_seconds()
    return max(0, int(left + 0.999))


def risk_line(request: dict[str, Any], language: str = "de", left: int | None = None) -> str:
    """'Risiko hoch · Noch 60 Sekunden. Ohne Antwort tut Nox es nicht.'"""
    lang = _lang(language)
    t = _TEXT[lang]
    parts = []
    risk = str(request.get("risk", ""))
    if risk:
        parts.append(t["risk"].format(risk=_RISKS[lang].get(risk, risk)))
    if left is not None:
        parts.append(t["left"].format(n=left) if left > 0 else t["over"])
    return " · ".join(parts)


def remember_label(request: dict[str, Any], language: str = "de") -> str | None:
    """'Für 8 Stunden erlauben', from how long the core keeps the grant (the dashboard's
    `rememberLabel`); None when it keeps none - high or critical risk, or the core did not say -
    and then it is not offered. Never "always"."""
    if not rememberable(request):
        return None
    try:
        seconds = float(request.get("remember_ttl_s") or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    t = _TEXT[_lang(language)]
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = int(seconds // 3600)
        return t["remember_hour"] if hours == 1 else t["remember_hours"].format(n=hours)
    minutes = max(1, round(seconds / 60))
    return t["remember_minute"] if minutes == 1 else t["remember_minutes"].format(n=minutes)


class PermissionDialog:
    """Wraps a QMessageBox so tests can inspect it offscreen without exec."""

    def __init__(
        self,
        request: dict[str, Any],
        *,
        language: str = "de",
        parent: QWidget | None = None,
        node_control: bool = False,
    ) -> None:
        self.request = request
        self.language = language
        t = _TEXT[_lang(language)]
        self.box = QMessageBox(parent)
        self.box.setIcon(QMessageBox.Icon.Question)
        self.box.setWindowTitle(question(request, language))
        self.box.setTextFormat(Qt.TextFormat.RichText)
        self.box.setText(sentence(request, language, rich=True))
        self._satellite = voice_satellite_line(request, language)
        self.update_left()
        device, target = permission_device(request)
        details = [
            f"{t['agent']}: {request.get('agent', '?')}",
            f"{t['tool']}: {request.get('tool', '?')}",
            f"{t['action']}: {request.get('action', '?')}",
        ]
        if device:
            details.append(f"{t['device']}: {device}")
        if target:
            details.append(f"{t['target']}: {target}")
        if request.get("mode"):
            details.append(f"{t['mode']}: {request['mode']}")
        self.box.setDetailedText("\n".join(details))
        # While Nox controls mouse and keyboard on a paired PC, "allow" needs the security PIN
        # (`nox.node.world_routes.session_refusal`); otherwise there is no field at all.
        self.node_control = node_control
        self.pin_label = QLabel(t["pin"])
        self.pin = QLineEdit()
        self.pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin.setToolTip(t["pin_hint"])
        self.pin_label.setBuddy(self.pin)
        if node_control:
            layout: Any = self.box.layout()
            if layout is not None:
                row = layout.rowCount()
                layout.addWidget(self.pin_label, row, 0, 1, layout.columnCount())
                layout.addWidget(self.pin, row + 1, 0, 1, layout.columnCount())
        else:
            self.pin_label.setVisible(False)
            self.pin.setVisible(False)
        self.allow_button = self.box.addButton(t["allow"], QMessageBox.ButtonRole.AcceptRole)
        self.deny_button = self.box.addButton(t["deny"], QMessageBox.ButtonRole.RejectRole)
        label = remember_label(request, language)
        # As long as the core keeps the grant - never "always", never at high risk.
        self.remember_button = (
            self.box.addButton(label, QMessageBox.ButtonRole.YesRole) if label else None
        )
        self.box.setDefaultButton(self.deny_button)
        self.box.setEscapeButton(self.deny_button)
        self.timer = QTimer(self.box)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

    def update_left(self, now: datetime | None = None) -> int | None:
        """Refresh the risk and countdown line; returns the seconds left (None: not known)."""
        left = seconds_left(self.request, now)
        line = risk_line(self.request, self.language, left)
        self.box.setInformativeText("\n".join(x for x in (self._satellite, line) if x))
        return left

    def _tick(self) -> None:
        if self.update_left() == 0:
            # The core has said no by itself: there is nothing left to answer.
            self.timer.stop()
            self.box.reject()

    def decision(self, allowed: bool, *, remember: bool = False) -> dict[str, Any]:
        """Reply payload for the chosen answer."""
        return permission_reply(
            self.request,
            allow=allowed,
            remember=remember,
            pin=self.pin.text() if self.node_control else "",
        )

    def run(self) -> dict[str, Any]:
        """Modal; returns the `security.permission.reply` payload. Closing the box means deny."""
        if seconds_left(self.request) is not None:
            self.timer.start()
        self.box.exec()
        self.timer.stop()
        clicked = self.box.clickedButton()
        if self.remember_button is not None and clicked is self.remember_button:
            return self.decision(True, remember=True)
        return self.decision(clicked is self.allow_button)
