"""The sentences of the question on the device: what a person at the device is asked before a call
with a side effect runs, built from the call itself and never from text the core wrote."""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from nox.node.device.cli_text import language
from nox.node.wire import protocol as p

#: Longest question the dialog shows; anything longer is refused, never cut.
MAX_CONFIRM_CHARS = 1200


#: What each tool does, in one plain sentence for the person at the device. A tool
#: that is not listed is named instead (`DIALOG_FALLBACK`).
DIALOG_SENTENCES: dict[str, dict[str, str]] = {
    "de": {
        "file.list": "Nox möchte sehen, was in einem Ordner auf diesem Gerät liegt.",
        "file.read": "Nox möchte eine Datei auf diesem Gerät lesen.",
        "file.write": "Nox möchte eine Datei auf diesem Gerät schreiben.",
        "shell.run": "Nox möchte auf diesem Gerät einen Befehl ausführen (abgeschirmt).",
        "screen.capture": "Nox möchte ein Bildschirmfoto dieses Geräts machen und es an Nox "
        "zu Hause schicken.",
        "camera.snapshot": "Nox möchte mit der Kamera dieses Geräts ein Bild aufnehmen und es "
        "an Nox zu Hause schicken.",
        "computer.session_start": "Nox steuert bis {minutes} min Maus und Tastatur und kann in "
        "dieser Zeit alles, was du an diesem PC kannst.",
        "desktop.process_stop": "Nox möchte ein Programm auf diesem Gerät beenden; was darin "
        "nicht gespeichert ist, geht verloren.",
        "desktop.type_text": "Nox möchte Text in ein Fenster auf diesem Gerät tippen.",
        "desktop.window_close": "Nox möchte ein Fenster auf diesem Gerät schließen.",
        "browser.click": "Nox möchte in seinem eigenen Browser auf diesem Gerät etwas anklicken.",
        "browser.type": "Nox möchte in seinem eigenen Browser auf diesem Gerät etwas eintippen.",
        "browser.submit": "Nox möchte in seinem eigenen Browser auf diesem Gerät ein Formular "
        "absenden.",
        "notify.show": "Nox möchte auf diesem Gerät einen Hinweis zeigen.",
        "media.control": "Nox möchte die Musik auf diesem Gerät steuern.",
        "voice.say": "Nox möchte auf diesem Gerät etwas sagen.",
        "obs.request": "Nox möchte OBS auf diesem Gerät steuern.",
    },
    "en": {
        "file.list": "Nox wants to see what is in a folder on this device.",
        "file.read": "Nox wants to read a file on this device.",
        "file.write": "Nox wants to write a file on this device.",
        "shell.run": "Nox wants to run a command on this device (sandboxed).",
        "screen.capture": "Nox wants to take a screenshot of this device and send it to the "
        "Nox at home.",
        "camera.snapshot": "Nox wants to take a picture with this device's camera and send it "
        "to the Nox at home.",
        "computer.session_start": "Nox controls mouse and keyboard for up to {minutes} min and "
        "can do anything you can do at this PC during that time.",
        "desktop.process_stop": "Nox wants to end a program on this device; whatever is not "
        "saved in it is lost.",
        "desktop.type_text": "Nox wants to type text into a window on this device.",
        "desktop.window_close": "Nox wants to close a window on this device.",
        "browser.click": "Nox wants to click something in its own browser on this device.",
        "browser.type": "Nox wants to type something in its own browser on this device.",
        "browser.submit": "Nox wants to send a form in its own browser on this device.",
        "notify.show": "Nox wants to show a notice on this device.",
        "media.control": "Nox wants to control the music on this device.",
        "voice.say": "Nox wants to say something on this device.",
        "obs.request": "Nox wants to control OBS on this device.",
    },
}


DIALOG_FALLBACK = {
    "de": "Nox möchte auf diesem Gerät „{tool}“ ausführen.",
    "en": "Nox wants to run “{tool}” on this device.",
}


DIALOG_DETAILS = {"de": "Genau:", "en": "Exactly:"}


DIALOG_TAINTED = {
    "de": "Achtung: Die Anfrage kam aus einem Gespräch mit fremden Inhalten (Web, Dateien, "
    "Nachrichten). Nur erlauben, wenn du das selbst wolltest.",
    "en": "Careful: the request came from a conversation with content from elsewhere (web, "
    "files, messages). Allow it only if you wanted this yourself.",
}


DIALOG_QUESTION = {
    "de": "Erlauben? (Ohne Antwort in 60 s: Ablehnen)",
    "en": "Allow? (No answer in 60 s: deny)",
}


#: The session length the dialog names when the call leaves it to the device's setting.
DEFAULT_SESSION_MINUTES = 15


def dialog_sentence(tool: str, arguments: dict[str, Any], lang: str) -> str:
    """The one plain sentence that opens the device's question about `tool`."""
    words = DIALOG_SENTENCES.get(lang, DIALOG_SENTENCES["en"])
    sentence = words.get(tool)
    if sentence is None:
        return DIALOG_FALLBACK.get(lang, DIALOG_FALLBACK["en"]).format(tool=p.escape_controls(tool))
    if tool == "computer.session_start":
        minutes = arguments.get("minutes")
        valid = isinstance(minutes, int) and not isinstance(minutes, bool) and 0 < minutes <= 120
        return sentence.format(minutes=minutes if valid else DEFAULT_SESSION_MINUTES)
    return sentence


def confirmation_text(
    tool: str, arguments: dict[str, Any], *, tainted: bool = False, lang: str | None = None
) -> str | None:
    """The question for the person at the device: one plain sentence for what the tool does
    (`DIALOG_SENTENCES`, in this device's language), then every argument in full, one per line,
    rendered exactly as the core's permission target renders it (`protocol.render`: quoted,
    look-alikes, bidi and invisible characters escaped, runs of spaces counted). None when it
    would be too long to read - then nothing is asked and the call is refused (no silent
    truncation)."""

    chosen = lang or language()
    try:
        json.dumps(arguments, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, TypeError, ValueError):
        return None  # not valid Unicode (a lone surrogate): refused, not shown escaped
    lines = [dialog_sentence(tool, arguments, chosen)]
    # The tool's own name always, so the sentence can be checked against it.
    lines.append(f"{DIALOG_DETAILS.get(chosen, DIALOG_DETAILS['en'])} {p.escape_controls(tool)}")
    for key, value in arguments.items():
        lines.append(f"  {p.escape_controls(str(key))}: {p.render(value)}")
    if tainted:
        lines.append(DIALOG_TAINTED.get(chosen, DIALOG_TAINTED["en"]))
    lines.append(DIALOG_QUESTION.get(chosen, DIALOG_QUESTION["en"]))
    text = unicodedata.normalize("NFC", "\n".join(lines))
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None  # not valid Unicode: nothing to show honestly
    return text if len(text) <= MAX_CONFIRM_CHARS else None
