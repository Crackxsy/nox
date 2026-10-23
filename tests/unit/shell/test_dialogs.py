"""The desktop permission dialog says what the dashboard's prompt says (H4). Made-up names only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from nox.shell.dialogs import (
    PermissionDialog,
    question,
    remember_label,
    risk_line,
    seconds_left,
    sentence,
)
from nox.shell.logic import permission_device

REQ = {
    "request_id": "req-7",
    "agent": "coder",
    "tool": "shell",
    "action": "run",
    "mode": "coding",
    "risk": "high",
    "target": "git push",
}

NODE_REQ = {
    "request_id": "req-8",
    "agent": "companion",
    "tool": "file",
    "action": "delete",
    "mode": "companion",
    "risk": "high",
    "target": "Laptop (3f2a): Archiv/neu.zip",
    "device_id": "3f2a" * 8,
    "device_name": "Laptop",
}


def test_dialog_asks_the_dashboards_question_with_the_device_on_its_own(qapp: Any) -> None:
    d = PermissionDialog(NODE_REQ, language="de")
    assert d.box.windowTitle() == "Darf Nox etwas löschen?"
    assert d.box.text() == "Nox möchte auf <b>Laptop</b> löschen: Laptop (3f2a): Archiv/neu.zip"
    details = d.box.detailedText()
    assert "Gerät: Laptop" in details and "Wer fragt: companion" in details
    assert d.box.defaultButton() is d.deny_button
    assert d.box.escapeButton() is d.deny_button


def test_dialog_never_takes_a_device_out_of_the_target() -> None:
    spoof = {**REQ, "action": "delete", "target": "Arbeits-PC (ab12): harmlos.txt"}
    assert permission_device(spoof) == ("", "Arbeits-PC (ab12): harmlos.txt")
    assert sentence(spoof, "de") == "Nox möchte löschen: Arbeits-PC (ab12): harmlos.txt"
    assert permission_device({**spoof, "device_name": "Laptop"})[0] == ""  # no id: no device


def test_dialog_escapes_names_and_targets(qapp: Any) -> None:
    evil = {**NODE_REQ, "device_name": "<i>PC</i>", "target": "<script>x</script>"}
    text = sentence(evil, "de", rich=True)
    assert "<script>" not in text and "&lt;i&gt;PC&lt;/i&gt;" in text


def test_dialog_says_the_risk_and_counts_down(qapp: Any) -> None:
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    timed = {**NODE_REQ, "expires_at": (now + timedelta(seconds=60)).isoformat()}
    assert seconds_left(timed, now) == 60
    line = risk_line(timed, "de", 60)
    assert line == "Risiko hoch · Noch 60 Sekunden. Ohne Antwort tut Nox es nicht."
    assert risk_line(timed, "de", 0).endswith("Die Zeit ist um – ohne Antwort tut Nox es nicht.")
    assert seconds_left(REQ) is None
    d = PermissionDialog(timed, language="de")
    assert d.update_left(now) == 60
    assert "Noch 60 Sekunden" in d.box.informativeText()


def test_dialog_offers_the_dashboards_remember_label_only_below_high_risk(qapp: Any) -> None:
    d = PermissionDialog(REQ, language="de")
    assert d.remember_button is None  # high risk: confirmed every time
    medium = {**REQ, "risk": "medium", "remember_ttl_s": 8 * 3600}
    assert remember_label(medium, "de") == "Für 8 Stunden erlauben"
    assert remember_label(medium, "en") == "Allow for 8 hours"
    assert remember_label({**medium, "remember_ttl_s": 0}, "de") is None
    d = PermissionDialog(medium, language="de")
    assert d.remember_button is not None and d.remember_button.text() == "Für 8 Stunden erlauben"
    assert d.decision(True, remember=True) == {
        "grant_id": "req-7",
        "decision": "allow",
        "remember": True,
    }
    assert d.decision(False) == {"grant_id": "req-7", "decision": "deny", "remember": False}


def test_dialog_english_strings(qapp: Any) -> None:
    d = PermissionDialog(NODE_REQ, language="en")
    assert d.allow_button.text() == "Allow" and d.deny_button.text() == "Deny"
    assert d.box.windowTitle() == "May Nox delete something?"
    assert question(REQ, "en") == "May Nox do this?"
    assert d.box.text() == "On <b>Laptop</b>, Nox wants to delete: Laptop (3f2a): Archiv/neu.zip"


def test_dialog_asks_for_the_pin_only_while_nox_controls_a_pc(qapp: Any) -> None:
    d = PermissionDialog(REQ, language="de")
    assert not d.node_control
    d.pin.setText("4711")
    assert "pin" not in d.decision(True)  # no field shown: nothing sent
    d = PermissionDialog(REQ, language="de", node_control=True)
    assert d.pin.echoMode() == d.pin.EchoMode.Password
    assert d.pin_label.text() == "Sicherheits-PIN"
    d.pin.setText(" 4711 ")
    assert d.decision(True) == {
        "grant_id": "req-7",
        "decision": "allow",
        "remember": False,
        "pin": "4711",
    }
    assert "pin" not in d.decision(False)  # a "deny" never carries it
