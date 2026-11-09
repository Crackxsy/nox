"""Native confirmation dialog for `security.permission_requested` (Process Model, shell duties).

The dialog only collects a decision; the core enforces it. Voice never confirms here.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit, QMessageBox, QWidget

from nox.shell.logic import permission_reply, rememberable, voice_satellite_line

_TEXT = {
    "de": {
        "title": "Nox – Berechtigung",
        "body": "{agent} möchte {tool} / {action} ausführen.",
        "target": "Ziel: {target}",
        "risk": "Risiko: {risk}  ·  Modus: {mode}",
        "remember": "Für diese Sitzung merken",
        "allow": "Erlauben",
        "deny": "Ablehnen",
        "pin": "PIN (nur nötig, während Nox einen PC steuert)",
    },
    "en": {
        "title": "Nox – permission",
        "body": "{agent} wants to run {tool} / {action}.",
        "target": "Target: {target}",
        "risk": "Risk: {risk}  ·  Mode: {mode}",
        "remember": "Remember for this session",
        "allow": "Allow",
        "deny": "Deny",
        "pin": "PIN (only needed while Nox controls a PC)",
    },
}


class PermissionDialog:
    """Wraps a QMessageBox so tests can inspect it offscreen without exec."""

    def __init__(
        self, request: dict[str, Any], *, language: str = "de", parent: QWidget | None = None
    ) -> None:
        self.request = request
        t = _TEXT["de" if language.startswith("de") else "en"]
        self.box = QMessageBox(parent)
        self.box.setIcon(QMessageBox.Icon.Question)
        self.box.setWindowTitle(t["title"])
        self.box.setText(
            t["body"].format(
                agent=request.get("agent", "?"),
                tool=request.get("tool", "?"),
                action=request.get("action", "?"),
            )
        )
        details = []
        satellite = voice_satellite_line(request, language)
        if satellite:
            details.append(satellite)
        if request.get("target"):
            details.append(t["target"].format(target=request["target"]))
        details.append(
            t["risk"].format(risk=request.get("risk", "?"), mode=request.get("mode", "?"))
        )
        self.box.setInformativeText("\n".join(details))
        self.remember = QCheckBox(t["remember"])
        if rememberable(request):
            self.box.setCheckBox(self.remember)
        else:
            # High risk is confirmed every time; offering the box would promise what the core
            # refuses to do.
            self.remember.setVisible(False)
        # While Nox controls mouse and keyboard on a paired PC, "allow" needs the security PIN
        # (`nox.node.world_routes.session_refusal`); otherwise the field may stay empty.
        self.pin_label = QLabel(t["pin"])
        self.pin = QLineEdit()
        self.pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_label.setBuddy(self.pin)
        layout: Any = self.box.layout()
        if layout is not None:
            row = layout.rowCount()
            layout.addWidget(self.pin_label, row, 0, 1, layout.columnCount())
            layout.addWidget(self.pin, row + 1, 0, 1, layout.columnCount())
        self.allow_button = self.box.addButton(t["allow"], QMessageBox.ButtonRole.AcceptRole)
        self.deny_button = self.box.addButton(t["deny"], QMessageBox.ButtonRole.RejectRole)
        self.box.setDefaultButton(self.deny_button)
        self.box.setEscapeButton(self.deny_button)

    def decision(self, allowed: bool) -> dict[str, Any]:
        """Reply payload for the chosen button and the checkbox state."""
        return permission_reply(
            self.request, allow=allowed, remember=self.remember.isChecked(), pin=self.pin.text()
        )

    def run(self) -> dict[str, Any]:
        """Modal; returns the `security.permission.reply` payload. Closing the box means deny."""
        self.box.exec()
        return self.decision(self.box.clickedButton() is self.allow_button)
