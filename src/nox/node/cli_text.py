"""The node's terminal words: German and English, the command's real name, the group labels, and
what this device can do at all.

* **Language** - `NOX_LANG` (`de` or `en`) wins, then the system locale (`LC_ALL`, `LC_MESSAGES`,
  `LANG`, the Windows UI language); German only when one of them says so, English otherwise.
* **The command** - what a person types to reach this node: `nox-node` (the Windows installer
  and the desktop install scripts put it on the PATH), `sudo nox-node` on a headless Linux node,
  whose files belong to the service user `nox-node` (`scripts/install-linux.sh --headless`).
* **Labels** - one set, `nox.node.manifest.GROUPS`, the same words the Devices page shows.
* **What exists here** - `supported_here(group)`: computer use and the window tools on Windows
  only, music and commands not on macOS, installing programs on no node yet. `allow` refuses the
  rest honestly instead of switching on a group that can never do anything.
* **Yes or no** - `[j/N]` in German and `[y/N]` in English; both answers are understood in both
  languages, and anything else (an empty line included) is no.
"""

from __future__ import annotations

import getpass
import locale
import os
import sys
from collections.abc import Callable

from nox.node.manifest import GROUPS

__all__ = [
    "ask_yes",
    "command",
    "group_label",
    "language",
    "node_command",
    "supported_here",
    "t",
    "unsupported_reason",
]

LANG_ENV = "NOX_LANG"
#: Where a headless Linux node keeps its files (`scripts/install-linux.sh --headless`).
HEADLESS_HOME = "/var/lib/nox-node"
HEADLESS_USER = "nox-node"
YES_WORDS = frozenset({"j", "ja", "y", "yes"})


def language() -> str:
    """`de` or `en` for this terminal."""
    wanted = os.environ.get(LANG_ENV, "").strip().lower()[:2]
    if wanted in ("de", "en"):
        return wanted
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(name, "").strip().lower()
        if value and value not in ("c", "posix") and not value.startswith("c."):
            return "de" if value.startswith("de") else "en"
    try:
        found = locale.getlocale()[0] or ""
    except ValueError:
        found = ""
    return "de" if found.lower().startswith(("de", "german")) else "en"


def headless(platform: str = sys.platform) -> bool:
    """A headless Linux node: its folder under `/var/lib/nox-node`, or run as its service user."""
    if not platform.startswith("linux"):
        return False
    home = os.environ.get("NOX_NODE_HOME", "")
    if home.startswith(HEADLESS_HOME):
        return True
    try:
        return getpass.getuser() == HEADLESS_USER
    except (OSError, KeyError, ImportError):
        return False


def node_command(platform: str = sys.platform) -> str:
    """What a person types for this node's commands."""
    return "sudo nox-node" if headless(platform) else "nox-node"


def command(rest: str) -> str:
    """`nox-node <rest>` as this device needs it (`sudo nox-node <rest>` on a headless node)."""
    return f"{node_command()} {rest}".strip()


def group_label(group: str, lang: str | None = None) -> str:
    info = GROUPS.get(group)
    if info is None:
        return group
    return info.label_de if (lang or language()) == "de" else info.label_en


#: Groups that cannot work on some systems - the tools exist for one platform only.
_ONLY_ON: dict[str, tuple[str, ...]] = {
    "computer": ("win32",),
    "desktop": ("win32",),
    "media": ("win32", "linux"),
    "shell": ("win32", "linux"),
}
#: Groups no node can use yet.
NOT_YET: frozenset[str] = frozenset({"packages"})


def unsupported_reason(group: str, platform: str = sys.platform) -> str | None:
    """`not_yet` (no node has it yet), `not_here` (not on this system) or None."""
    if group in NOT_YET:
        return "not_yet"
    only = _ONLY_ON.get(group)
    if only is not None and not any(platform.startswith(p) for p in only):
        return "not_here"
    return None


def supported_here(group: str, platform: str = sys.platform) -> bool:
    """Whether the group can do anything on this device at all."""
    return unsupported_reason(group, platform) is None


def ask_yes(
    prompt: str,
    *,
    lang: str | None = None,
    read: Callable[[str], str] = input,
) -> bool:
    """One question, answered in the terminal: yes only for `j`, `ja`, `y` or `yes`."""
    hint = "[j/N]" if (lang or language()) == "de" else "[y/N]"
    try:
        answer = read(f"{prompt} {hint} ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in YES_WORDS


TEXTS: dict[str, dict[str, str]] = {
    "de": {
        # pairing
        "pair_bad_link": "Kein Nox-Kopplungslink: {error}",
        "pair_ok": "Gekoppelt als „{name}“. Starten: {run}",
        "pair_again_not_paired": "Dieses Gerät ist nicht gekoppelt. Koppeln: {pair}",
        "pair_again_not_needed": (
            "Dieses Gerät ist gekoppelt und sein Zertifikat gilt noch. Neu koppeln geht nur, "
            "wenn es abgelaufen ist, entfernt wurde oder nicht mehr zum Nox passt. Erst am Nox "
            "zu Hause unter Geräte entfernen, dann hier: {again}"
        ),
        "pair_again_ok": "Alte Kopplung abgelegt ({why}). Das Protokoll und der Bestätigungs"
        "schlüssel bleiben.",
        "fingerprint_mismatch": "Das antwortende Gerät ist nicht das Nox aus dem Link (sein "
        "Fingerabdruck ist anders). Es wurde nichts gesendet. Nur im eigenen Netz koppeln.",
        "unreachable": "Nox ist unter der Adresse aus dem Link nicht erreichbar.",
        "already_paired": "Dieses Gerät ist schon gekoppelt. Neu koppeln, wenn das Zertifikat "
        "abgelaufen ist oder Nox es entfernt hat: {again}",
        "used": "Der Code wurde schon benutzt. Einen neuen erzeugen.",
        "expired": "Der Code ist abgelaufen. Einen neuen erzeugen.",
        "unknown_code": "Nox kennt diesen Code nicht.",
        "max_devices": "Nox hat schon so viele Geräte, wie es annimmt.",
        "rate_limited": "Zu viele Versuche. Eine Minute warten.",
        "name_taken": "Der Name ähnelt einem vorhandenen Gerät. Einen anderen mit --name wählen.",
        "invalid_characters": "Bitte nur Buchstaben (auch ä ö ü ß), Ziffern, Leerzeichen und "
        "- _ . verwenden. Einen anderen Namen mit --name wählen.",
        "not_nox": "Das Gerät unter dieser Adresse antwortet nicht wie Nox.",
        "key_not_protected": "Die Schlüsseldatei ließ sich nicht nur für diesen Benutzer lesbar "
        "machen.",
        "pair_failed": "Koppeln hat nicht geklappt: {reason}",
        "no_confirmation_key": "Auf diesem Gerät ließ sich kein Bestätigungsschlüssel ablegen.",
        # run
        "not_paired": "Dieses Gerät ist noch nicht gekoppelt: {pair}",
        "revoked": "Von Nox entfernt. Schlüssel und Zertifikat sind gelöscht.",
        "pin_mismatch": "Die gespeicherte Zertifizierungsstelle ist nicht die gekoppelte. Neu "
        "koppeln: {again}",
        "stopped": "Beendet.",
        "media_notice": "Nox darf auf diesem Gerät jetzt Musik steuern (abspielen, pausieren, "
        "Lautstärke). Ausschalten: {deny}",
        # update
        "update_ask": "Nox {version} jetzt auf diesem Gerät installieren?",
        "update_not_installed": "Nicht installiert.",
        "update_restart_soon": "Der Knoten startet die neue Version innerhalb einer Minute.",
        "update_start_by_hand": "Den Knoten starten: {run}",
        "update_stale": "Achtung: seit {days} Tagen keine neuere signierte Version gesehen. "
        "Updates werden vielleicht zurückgehalten (Spiegel oder Netz); die Release-Seite des "
        "Projekts prüfen.",
        # status
        "status_name": "Name:      {value}",
        "status_core": "Nox:       {value}",
        "status_state": "Zustand:   {value}",
        "status_may": "Nox darf:  {value}",
        "status_risk": "Höchstes Risiko: {value}",
        "status_folders": "Ordner:    {value}",
        "status_update": "Update:    {value}",
        "status_nothing": "nichts",
        "status_none": "keine",
        "status_not_here": "auf diesem Gerät nicht verfügbar",
        "status_not_running": "läuft nicht",
        "status_removed": "Von Nox entfernt.",
        "status_unpaired": "Nicht gekoppelt.",
        "status_session": "Sitzung:   Nox steuert gerade Maus und Tastatur",
        "log_intact": "Kette intakt.",
        "log_broken": "KETTE GEBROCHEN - das Protokoll wurde verändert.",
        # folders
        "folder_absolute": "Einen absoluten Ordner auf diesem Gerät angeben (kein Netzwerkpfad).",
        "folder_missing": "{path} ist kein Ordner.",
        "folder_home": "Den eigenen Ordner des Knotens kann Nox nie öffnen.",
        "folder_broad": "Dieser Ordner ist zu weit (ein Laufwerk, dein Benutzerordner, ein "
        "System- oder App-Daten-Ordner). Einen Ordner darin erlauben, etwa einen für Nox.",
        "folder_secret": "Dieser Ordner enthält einen Schlüssel- oder Passwortspeicher oder "
        "liegt darin.",
        "folder_marked": "Dieser Ordner ist (oder liegt in) einem Repository oder einem "
        "App-Arbeitsbereich wie einem Obsidian-Tresor, dessen Dateien Code ausführen können. "
        "Einen einfachen Ordner außerhalb erlauben.",
        # games
        "game_name": "Den Programmnamen angeben, zum Beispiel Game.exe (ohne Pfad).",
        "game_interactive": "Ein Spiel entfernen geht nur in einem Terminal auf diesem Gerät.",
        "game_ask": "Darf Nox hier Maus und Tastatur benutzen, während {game} läuft?",
        "game_not_removed": "Nicht entfernt.",
        "game_no_manifest": "Auf diesem Gerät gibt es keine bestätigte Freigabe; nichts entfernt.",
        "game_not_recorded": "Die Änderung ließ sich auf diesem Gerät nicht festhalten; nichts "
        "entfernt.",
        "game_watching": "Wird beobachtet: {game}.",
        "game_unwatched": "Wird nicht mehr beobachtet: {game}.",
        # allow
        "allow_unknown": "Unbekannte Gruppe „{group}“. Bekannt: {known}",
        "allow_folder_group": "--folder gehört zur Gruppe files.",
        "allow_interactive": "Erlauben geht nur in einem Terminal auf diesem Gerät.",
        "allow_not_here": "{label}: auf diesem Gerät nicht verfügbar.",
        "allow_not_yet": "{label}: auf Geräten noch nicht verfügbar.",
        "allow_ask": "Darf Nox auf diesem Gerät: {label}?",
        "allow_ask_folder": "Nox darf dann Dateien in {folder} lesen und schreiben. Erlauben?",
        "allow_computer": "Nox steuert dann in Sitzungen, die du hier einzeln erlaubst, Maus und "
        "Tastatur und kann in dieser Zeit alles, was du an diesem PC kannst (nie, während ein "
        "Spiel oder ein Anti-Cheat-Dienst läuft; {stop} beendet sofort). Wirklich erlauben?",
        "allow_trust": "von einer Person am Nox bestätigte Aktionen mit hohem Risiko ausführen "
        "(hier ohne Bildschirm)",
        "allow_files_folder": "Dateien in {folder} lesen und schreiben",
        "allow_refused": "Nicht erlaubt.",
        "allow_done": "Erlaubt: {label}.",
        "deny_done": "Verboten: {label}.",
        "allow_files_no_folder": "Dateien: an, aber noch kein Ordner erlaubt. Einen erlauben: "
        "{add}",
        # computer-use session on this device
        "session_refused": "Nox steuert gerade Maus und Tastatur auf diesem Gerät. Solange die "
        "Sitzung läuft, lässt sich hier nichts freischalten. Erst beenden ({stop} oder {cmd}), "
        "dann noch einmal.",
        "session_unreadable": "Ob Nox gerade Maus und Tastatur steuert, lässt sich nicht lesen "
        "({path}). Deshalb wird nichts freigeschaltet. Den Knoten beenden ({cmd}) und neu "
        "starten, dann noch einmal.",
        "session_dialog": "Nox steuert bis {minutes} min Maus und Tastatur und kann in dieser "
        "Zeit alles, was du an diesem PC kannst. {stop} beendet sofort.",
        # stop / resume
        "stop_done": "Angehalten. Auf diesem Gerät fortsetzen mit: {resume}",
        "resume_done": "Auf diesem Gerät fortgesetzt.",
        # satellites
        "voice_muted": "Mikrofon auf diesem Gerät stummgeschaltet. Hier wieder einschalten mit: "
        "{unmute}",
        "voice_unmuted": "Mikrofon auf diesem Gerät wieder eingeschaltet.",
        "voice_on": "auf diesem Gerät erlaubt",
        "voice_off": "aus ({allow})",
        "voice_line": "Sprache:     {value}",
        "voice_mode": "Modus:       {value}",
        "voice_muted_here": "Stumm hier: {value}",
        "voice_running": "Läuft:       {value}",
        "voice_mic": "Mikrofon:    {value}",
        "voice_core": "Nox sagt:    nein ({value})",
        "voice_problem": "Problem:     {value}",
        "yes": "ja",
        "no": "nein",
        "mic_open": "offen",
        "mic_closed": "zu",
        "released": "Losgelassen.",
        "obs_no_keyring": "Auf diesem Gerät gibt es keinen Schlüsselbund: das OBS-Passwort kann "
        "nicht gespeichert werden.",
        "obs_cleared": "OBS-Passwort aus dem Schlüsselbund dieses Geräts entfernt.",
        "obs_no_password": "Kein Passwort angegeben; nichts gespeichert.",
        "obs_stored": "OBS-Passwort im Schlüsselbund dieses Geräts gespeichert (es geht nie an "
        "Nox).",
        "obs_resumed": "OBS nimmt wieder Änderungen von Nox an (die Panik-Sperre auf diesem "
        "Gerät ist aufgehoben).",
        "overlay_needs_obs": "(Braucht die Gruppe obs: {allow})",
    },
    "en": {
        "pair_bad_link": "Not a Nox node link: {error}",
        "pair_ok": "Paired as {name!r}. Start it with: {run}",
        "pair_again_not_paired": "This device is not paired. Pair it: {pair}",
        "pair_again_not_needed": (
            "This device is paired and its certificate is still valid. Pairing again works only "
            "when it expired, was removed or no longer matches the Nox. Remove it on the Nox at "
            "home under Devices first, then here: {again}"
        ),
        "pair_again_ok": "The old pairing is put aside ({why}). The log and the confirmation "
        "key stay.",
        "fingerprint_mismatch": "The device answering is not the Nox from the link (its "
        "fingerprint differs). Nothing was sent. Pair only in your own network.",
        "unreachable": "Nox is not reachable at the address in the link.",
        "already_paired": "This device is already paired. Pair again once its certificate "
        "expired or Nox removed it: {again}",
        "used": "The code was already used. Make a new one.",
        "expired": "The code has expired. Make a new one.",
        "unknown_code": "Nox does not know this code.",
        "max_devices": "Nox has as many devices as it accepts.",
        "rate_limited": "Too many attempts. Wait a minute.",
        "name_taken": "The name looks like a device Nox already has. Choose another one with "
        "--name.",
        "invalid_characters": "Use letters (ä ö ü ß too), digits, spaces and - _ . only. Choose "
        "another name with --name.",
        "not_nox": "The device at that address does not answer like Nox.",
        "key_not_protected": "The key file could not be made readable by this user only.",
        "pair_failed": "Pairing failed: {reason}",
        "no_confirmation_key": "No confirmation key could be stored on this device.",
        "not_paired": "This device is not paired yet: {pair}",
        "revoked": "Removed from Nox. Key and certificate are deleted.",
        "pin_mismatch": "The stored certificate authority is not the paired one. Pair again: "
        "{again}",
        "stopped": "Stopped.",
        "media_notice": "Nox may now control music on this device (play, pause, volume). "
        "Switch it off: {deny}",
        "update_ask": "Install Nox {version} on this device now?",
        "update_not_installed": "Not installed.",
        "update_restart_soon": "The node starts the new version within a minute.",
        "update_start_by_hand": "Start the node: {run}",
        "update_stale": "Warning: no newer signed release seen for {days} days. Updates may be "
        "held back (a frozen mirror or network); check the project's releases page.",
        "status_name": "Name:      {value}",
        "status_core": "Nox:       {value}",
        "status_state": "State:     {value}",
        "status_may": "Nox may:   {value}",
        "status_risk": "Max risk:  {value}",
        "status_folders": "Folders:   {value}",
        "status_update": "Update:    {value}",
        "status_nothing": "nothing",
        "status_none": "none",
        "status_not_here": "not available on this device",
        "status_not_running": "not running",
        "status_removed": "Removed from Nox.",
        "status_unpaired": "Not paired.",
        "status_session": "Session:   Nox is controlling mouse and keyboard",
        "log_intact": "Chain intact.",
        "log_broken": "CHAIN BROKEN - the log was altered.",
        "folder_absolute": "Give an absolute folder on this device (no network path).",
        "folder_missing": "{path} is not a folder.",
        "folder_home": "The node's own folder can never be opened by Nox.",
        "folder_broad": "That folder is too broad (a drive, your home folder, a system or "
        "app-data folder). Allow a folder inside it, for example one for Nox's files.",
        "folder_secret": "That folder holds or lies in a store of keys or passwords.",
        "folder_marked": "That folder is (or lies in) a repository or an app workspace such as "
        "an Obsidian vault, whose files can run code. Allow a plain folder outside it.",
        "game_name": "Give the program's file name, for example Game.exe (no path).",
        "game_interactive": "Removing a game needs an interactive terminal on this device.",
        "game_ask": "Let Nox use mouse and keyboard here while {game} runs?",
        "game_not_removed": "Not removed.",
        "game_no_manifest": "No confirmed manifest on this device; nothing was removed.",
        "game_not_recorded": "The change could not be recorded on this device; nothing removed.",
        "game_watching": "Watching: {game}.",
        "game_unwatched": "No longer watching: {game}.",
        "allow_unknown": "Unknown group {group!r}. Known: {known}",
        "allow_folder_group": "--folder belongs to the group files.",
        "allow_interactive": "Allowing needs an interactive terminal on this device.",
        "allow_not_here": "{label}: not available on this device.",
        "allow_not_yet": "{label}: not available on devices yet.",
        "allow_ask": "Let Nox {label} on this device?",
        "allow_ask_folder": "Nox may then read and write files in {folder}. Allow?",
        "allow_computer": "In sessions you allow here one by one, Nox then controls mouse and "
        "keyboard and can do anything you can do at this PC during that time (never while a "
        "game or an anti-cheat service runs; {stop} stops it at once). Really allow?",
        "allow_trust": "run high-risk actions a person confirmed on the Nox (no screen here)",
        "allow_files_folder": "read and write files in {folder}",
        "allow_refused": "Not allowed.",
        "allow_done": "Allowed: {label}.",
        "deny_done": "Denied: {label}.",
        "allow_files_no_folder": "Files: on, but no folder allowed yet. Allow one: {add}",
        "session_refused": "Nox is controlling mouse and keyboard on this device. While the "
        "session runs, nothing can be allowed here. End it first ({stop} or {cmd}), then try "
        "again.",
        "session_unreadable": "Whether Nox is controlling mouse and keyboard cannot be read "
        "({path}), so nothing is allowed. Stop the node ({cmd}) and start it again, then try "
        "again.",
        "session_dialog": "Nox controls mouse and keyboard for up to {minutes} min and can do "
        "anything you can do at this PC during that time. {stop} stops it at once.",
        "stop_done": "Stopped. Undo on this device with: {resume}",
        "resume_done": "Resumed on this device.",
        "voice_muted": "Microphone muted on this device. Unmute here with: {unmute}",
        "voice_unmuted": "Microphone unmuted on this device.",
        "voice_on": "allowed on this device",
        "voice_off": "off ({allow})",
        "voice_line": "Voice:       {value}",
        "voice_mode": "Mode:        {value}",
        "voice_muted_here": "Muted here:  {value}",
        "voice_running": "Running:     {value}",
        "voice_mic": "Microphone:  {value}",
        "voice_core": "Core says:   no ({value})",
        "voice_problem": "Problem:     {value}",
        "yes": "yes",
        "no": "no",
        "mic_open": "open",
        "mic_closed": "closed",
        "released": "Released.",
        "obs_no_keyring": "No keyring on this device: the OBS password cannot be stored.",
        "obs_cleared": "OBS password removed from this device's keyring.",
        "obs_no_password": "No password given; nothing was stored.",
        "obs_stored": "OBS password stored in this device's keyring (it never goes to Nox's core).",
        "obs_resumed": "OBS takes changes from Nox again (the panic latch is lifted on this "
        "device).",
        "overlay_needs_obs": "(Needs the group obs: {allow})",
    },
}


def t(key: str, lang: str | None = None, **values: object) -> str:
    """One sentence in this terminal's language (English when a key is missing there)."""
    chosen = lang or language()
    text = TEXTS.get(chosen, TEXTS["en"]).get(key) or TEXTS["en"][key]
    return text.format(**values) if values else text
