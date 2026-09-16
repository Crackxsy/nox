from __future__ import annotations

from typing import Any

from nox.shell.dialogs import PermissionDialog

REQ = {
    "request_id": "req-7",
    "agent": "coder",
    "tool": "shell",
    "action": "run",
    "mode": "coding",
    "risk": "high",
    "target": "git push",
}


def test_dialog_shows_request_and_builds_reply(qapp: Any) -> None:
    d = PermissionDialog(REQ, language="de")
    assert "coder" in d.box.text() and "shell" in d.box.text() and "run" in d.box.text()
    assert "git push" in d.box.informativeText() and "high" in d.box.informativeText()
    assert d.box.defaultButton() is d.deny_button
    assert d.decision(False) == {"grant_id": "req-7", "decision": "deny", "remember": False}
    d.remember.setChecked(True)
    assert d.decision(True) == {"grant_id": "req-7", "decision": "allow", "remember": True}


def test_dialog_english_strings(qapp: Any) -> None:
    d = PermissionDialog(REQ, language="en")
    assert d.allow_button.text() == "Allow" and d.deny_button.text() == "Deny"
    assert d.remember.text() == "Remember for this session"
