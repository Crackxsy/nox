"""`nox pair`, `nox devices`, `nox unpair`: devices from a terminal - the way on a server without
a screen.

It asks the running core (as the local shell, with the per-start session token only this account
can read) for a one-time code, and prints the pairing link, a QR code to scan and the authority's
fingerprint. Nothing here talks to the network; the phone does that. The text follows
`identity.ui_language` (German or English).
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import segno

from nox import __version__
from nox.ipc.client import IpcClient
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.server import WS_PATH, read_runtime_info
from nox.ipc.tokens import read_session_token

__all__ = [
    "MESSAGES",
    "PairingError",
    "devices_in_terminal",
    "pair_in_terminal",
    "pair_node_in_terminal",
    "render_node",
    "render",
    "unpair_in_terminal",
]

MESSAGES: dict[str, dict[str, str]] = {
    "de": {
        "not_running": "Nox läuft gerade nicht. Starten: sudo systemctl start nox",
        "no_answer": "Nox antwortet nicht ({error}). Protokoll: journalctl -u nox -n 50",
        "scan": "Mit der Kamera des Handys oder Tablets scannen, oder diesen Link öffnen:",
        "code": "Code (falls das Gerät danach fragt): {code}",
        "valid": "Gültig bis {time} Uhr, nur einmal.",
        "warning": "Der Browser warnt beim ersten Öffnen, weil Nox ein eigenes Zertifikat hat.",
        "compare": "Prüfen, dass der Fingerabdruck des Zertifikats so beginnt:",
        "proceed": "Dann trotzdem fortfahren. Ohne Warnung und als App geht es, wenn das Gerät\n"
        "  das Nox-Zertifikat installiert; das Dashboard zeigt nach dem Koppeln, wie.",
        "others": "Andere Adressen dieses Rechners:",
        "off": "Der Netzwerkzugriff ist aus. In user.yaml unter ipc: lan: enabled: true setzen,\n"
        "dann Nox neu starten (auf dem Pi: sudo systemctl restart nox).",
        "starting": "Der Netzwerkzugriff startet gerade. Gleich noch einmal versuchen.",
        "bad_pin": "Die PIN stimmt nicht.",
        "failed": "Koppeln geht gerade nicht: {error}",
        "pin_prompt": "Sicherheits-PIN",
        "none": "Noch kein Gerät gekoppelt. Eins koppeln: nox pair",
        "header": "Gekoppelte Geräte:",
        "row": "  {name}  (gekoppelt {paired}, zuletzt gesehen {seen})  [{id}]",
        "never": "nie",
        "removed_row": "  {name}  entfernt am {when}  [{id}]",
        "no_match": "Kein gekoppeltes Gerät heißt „{which}“. Liste: nox devices",
        "ambiguous": "Mehrere Geräte passen zu „{which}“; bitte die ID nehmen:",
        "unpaired": "{name} ist entfernt. Seine offenen Verbindungen sind beendet.",
        "node_run": "Auf dem anderen PC oder Laptop (mit installiertem Nox) ausführen:",
        "node_fp": "Der Link enthält den Fingerabdruck dieses Nox; er beginnt so:",
    },
    "en": {
        "not_running": "Nox is not running. Start it: sudo systemctl start nox",
        "no_answer": "Nox does not answer ({error}). Log: journalctl -u nox -n 50",
        "scan": "Scan this with the phone's or tablet's camera, or open this link:",
        "code": "Code (if the device asks for one): {code}",
        "valid": "Valid until {time}, once.",
        "warning": "The browser warns the first time, because Nox has a certificate of its own.",
        "compare": "Check that the certificate's fingerprint starts like this:",
        "proceed": "Then continue anyway. No warning, and installing it as an app, works once the\n"
        "  device installs the Nox certificate; the dashboard shows how after pairing.",
        "others": "Other addresses of this machine:",
        "off": "Network access is off. Set ipc: lan: enabled: true in user.yaml, then restart Nox\n"
        "(on the Pi: sudo systemctl restart nox).",
        "starting": "Network access is starting. Try again in a few seconds.",
        "bad_pin": "That PIN is not right.",
        "failed": "Pairing is not possible right now: {error}",
        "pin_prompt": "Security PIN",
        "none": "No device paired yet. Pair one: nox pair",
        "header": "Paired devices:",
        "row": "  {name}  (paired {paired}, last seen {seen})  [{id}]",
        "never": "never",
        "removed_row": "  {name}  removed {when}  [{id}]",
        "no_match": 'No paired device is called "{which}". List: nox devices',
        "ambiguous": 'Several devices match "{which}"; please use the id:',
        "unpaired": "{name} is removed. Its open connections have ended.",
        "node_run": "On the other PC or laptop (with Nox installed), run:",
        "node_fp": "The link carries this Nox's fingerprint; it starts like this:",
    },
}


class PairingError(RuntimeError):
    """A message for the person at the terminal, already in their language."""


def _text(language: str) -> dict[str, str]:
    return MESSAGES.get(language, MESSAGES["en"])


async def _call(
    runtime_dir: Path, request: str, payload: dict[str, Any], text: dict[str, str]
) -> dict[str, Any]:
    """One request to the running core, as the local shell (the session token is readable by this
    account only)."""
    info = read_runtime_info(runtime_dir)
    port = info.get("ws_port")
    host = info.get("host", "127.0.0.1")
    try:
        token = read_session_token(runtime_dir)
    except (OSError, ValueError):
        token = ""
    if not port or not token:
        raise PairingError(text["not_running"])
    client = IpcClient(
        f"ws://{host}:{port}{WS_PATH}", token, "shell", "shell:pair", client_version=__version__
    )
    try:
        await client.connect()
    except (OSError, IpcError) as exc:
        raise PairingError(text["no_answer"].format(error=exc)) from exc
    try:
        return await client.request(request, payload)
    finally:
        await client.close()


async def _request(
    runtime_dir: Path,
    name: str,
    pin: str | None,
    text: dict[str, str],
    request: str = "lan.pair.start",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name}
    if pin:
        payload["pin"] = pin
    return await _call(runtime_dir, request, payload, text)


def _terminal_qr(url: str) -> str:
    """A QR code made of block characters, with a light border so phones read it on dark and
    light terminals alike."""
    out = io.StringIO()
    segno.make(url, error="m").terminal(out=out, compact=True, border=2)
    return out.getvalue()


def render(
    result: dict[str, Any], *, language: str = "de", qr: Callable[[str], str] | None = None
) -> str:
    """What a person reads: QR code, link, code, how long it is valid, what to compare."""
    text = _text(language)
    url = str(result["url"])
    expires = datetime.fromisoformat(str(result["expires_at"])).astimezone()
    code = str(result["code"])
    lines = [
        "",
        (qr or _terminal_qr)(url),
        f"  {text['scan']}",
        f"    {url}",
        "",
        "  " + text["code"].format(code=f"{code[:4]} {code[4:]}"),
        "  " + text["valid"].format(time=f"{expires:%H:%M}"),
        "",
        f"  {text['warning']}",
        f"  {text['compare']}",
        f"    {str(result['fingerprint'])[:23]}",
        f"  {text['proceed']}",
    ]
    others = [str(u) for u in result.get("urls", [])[1:]]
    if others:
        lines += ["", f"  {text['others']}", *(f"    {u}" for u in others)]
    return "\n".join(lines) + "\n"


def _explain(exc: IpcError, text: dict[str, str]) -> str:
    """What went wrong, decided on the core's structured reason - never on its English wording."""
    reason = str(exc.details.get("reason", ""))
    if reason == "off":
        return text["off"]
    if reason == "starting":
        return text["starting"]
    if exc.code == ERR_PERMISSION:
        return text["bad_pin"]
    return text["failed"].format(error=exc.message)


def pair_in_terminal(
    runtime_dir: Path,
    *,
    ask_pin: Callable[[str], str],
    name: str = "",
    language: str = "de",
    say: Callable[[str], None] = print,
) -> None:
    """Ask the core for a code and print it. When the core wants the PIN, `ask_pin(prompt)` is
    called once and the request repeated with it."""
    text = _text(language)
    try:
        result = asyncio.run(_request(runtime_dir, name, None, text))
    except IpcError as exc:
        if exc.code != ERR_PERMISSION:
            raise PairingError(_explain(exc, text)) from exc
        try:
            result = asyncio.run(_request(runtime_dir, name, ask_pin(text["pin_prompt"]), text))
        except IpcError as again:
            raise PairingError(_explain(again, text)) from again
    say(render(result, language=language))


def render_node(result: dict[str, Any], *, language: str = "de") -> str:
    """`nox pair --node`: the command to run on the other machine, the code, the fingerprint."""
    text = _text(language)
    expires = datetime.fromisoformat(str(result["expires_at"])).astimezone()
    code = str(result["code"])
    lines = [
        "",
        f"  {text['node_run']}",
        f'    nox node pair "{result["link"]}"',
        "",
        "  " + text["code"].format(code=f"{code[:4]} {code[4:]}"),
        "  " + text["valid"].format(time=f"{expires:%H:%M}"),
        f"  {text['node_fp']}",
        f"    {str(result['ca_fingerprint'])[:23]}",
    ]
    others = [str(u) for u in result.get("links", [])[1:]]
    if others:
        lines += ["", f"  {text['others']}", *(f"    {u}" for u in others)]
    return "\n".join(lines) + "\n"


def pair_node_in_terminal(
    runtime_dir: Path,
    *,
    ask_pin: Callable[[str], str],
    name: str = "",
    language: str = "de",
    say: Callable[[str], None] = print,
) -> None:
    """`nox pair --node` on the core: a node code and link, with the PIN when one is set."""
    text = _text(language)
    try:
        result = asyncio.run(_request(runtime_dir, name, None, text, "node.pair.start"))
    except IpcError as exc:
        if exc.code != ERR_PERMISSION:
            raise PairingError(_explain(exc, text)) from exc
        try:
            pin = ask_pin(text["pin_prompt"])
            result = asyncio.run(_request(runtime_dir, name, pin, text, "node.pair.start"))
        except IpcError as again:
            raise PairingError(_explain(again, text)) from again
    say(render_node(result, language=language))


def _when(value: object, text: dict[str, str]) -> str:
    if not value:
        return text["never"]
    return f"{datetime.fromisoformat(str(value)).astimezone():%d.%m.%Y %H:%M}"


def devices_in_terminal(
    runtime_dir: Path, *, language: str = "de", say: Callable[[str], None] = print
) -> None:
    """List the paired devices, active ones first."""
    text = _text(language)
    try:
        listed = asyncio.run(_call(runtime_dir, "lan.devices.list", {}, text))["devices"]
    except IpcError as exc:
        raise PairingError(_explain(exc, text)) from exc
    if not listed:
        say(text["none"])
        return
    lines = [text["header"]]
    for device in sorted(listed, key=lambda d: (d["revoked_at"] is not None, d["name"].lower())):
        if device["revoked_at"]:
            lines.append(
                text["removed_row"].format(
                    name=device["name"], when=_when(device["revoked_at"], text), id=device["id"][:8]
                )
            )
        else:
            lines.append(
                text["row"].format(
                    name=device["name"],
                    paired=_when(device["paired_at"], text),
                    seen=_when(device["last_seen_at"], text),
                    id=device["id"][:8],
                )
            )
    say("\n".join(lines))


def unpair_in_terminal(
    runtime_dir: Path,
    which: str,
    *,
    language: str = "de",
    say: Callable[[str], None] = print,
    node: bool = False,
) -> None:
    """Remove a paired device - or with `node`, a `nox node` - by its name (any case) or the
    start of its id. The machine Nox runs on needs no PIN for this: removing only ever narrows
    access."""
    text = _text(language)
    listing, key, remove = (
        ("node.list", "nodes", "node.unpair")
        if node
        else ("lan.devices.list", "devices", "lan.unpair")
    )
    try:
        listed = asyncio.run(_call(runtime_dir, listing, {}, text))[key]
    except IpcError as exc:
        raise PairingError(_explain(exc, text)) from exc
    active = [d for d in listed if not (d.get("revoked_at") or d.get("revoked"))]
    wanted = which.strip().lower()
    matches = [d for d in active if d["name"].lower() == wanted] or [
        d for d in active if wanted and d["id"].startswith(wanted)
    ]
    if not matches:
        raise PairingError(text["no_match"].format(which=which))
    if len(matches) > 1:
        rows = [f"  {d['name']}  [{d['id'][:8]}]" for d in matches]
        raise PairingError("\n".join([text["ambiguous"].format(which=which), *rows]))
    device = matches[0]
    try:
        asyncio.run(_call(runtime_dir, remove, {"device_id": device["id"]}, text))
    except IpcError as exc:
        raise PairingError(_explain(exc, text)) from exc
    say(text["unpaired"].format(name=device["name"]))
