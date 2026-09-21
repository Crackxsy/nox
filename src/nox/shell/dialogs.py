"""Native confirmation dialog for `security.permission_requested` (Process Model, shell duties).

The dialog only collects a decision; the core enforces it. Voice never confirms here.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QMessageBox, QWidget

from nox.shell.logic import permission_reply

_TEXT = {
    "de": {
        "title": "Nox – Berechtigung",
        "body": "{agent} möchte {tool} / {action} ausführen.",
        "target": "Ziel: {target}",
        "risk": "Risiko: {risk}  ·  Modus: {mode}",
        "remember": "Für diese Sitzung merken",
        "allow": "Erlauben",
        "deny": "Ablehnen",
    },
    "en": {
        "title": "Nox – permission",
        "body": "{agent} wants to run {tool} / {action}.",
        "target": "Target: {target}",
        "risk": "Risk: {risk}  ·  Mode: {mode}",
        "remember": "Remember for this session",
        "allow": "Allow",
        "deny": "Deny",
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
        if request.get("target"):
            details.append(t["target"].format(target=request["target"]))
        details.append(
            t["risk"].format(risk=request.get("risk", "?"), mode=request.get("mode", "?"))
        )
        self.box.setInformativeText("\n".join(details))
        self.remember = QCheckBox(t["remember"])
        self.box.setCheckBox(self.remember)
        self.allow_button = self.box.addButton(t["allow"], QMessageBox.ButtonRole.AcceptRole)
        self.deny_button = self.box.addButton(t["deny"], QMessageBox.ButtonRole.RejectRole)
        self.box.setDefaultButton(self.deny_button)
        self.box.setEscapeButton(self.deny_button)

    def decision(self, allowed: bool) -> dict[str, Any]:
        """Reply payload for the chosen button and the checkbox state."""
        return permission_reply(self.request, allow=allowed, remember=self.remember.isChecked())

    def run(self) -> dict[str, Any]:
        """Modal; returns the `security.permission.reply` payload. Closing the box means deny."""
        self.box.exec()
        return self.decision(self.box.clickedButton() is self.allow_button)
