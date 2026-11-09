"""`nox node voice | obs | overlay` - the satellites' own terminal commands (C9).

All of them act on this device only, as the logged-in user: the microphone's mute switch (only
this device unmutes), push-to-talk from a terminal, the OBS password in this device's keyring,
lifting the OBS panic latch, and the overlay URL for an OBS browser source here.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from nox.node.home import NodeHome
from nox.node.manifest import load_manifest
from nox.node.obs import OBS_PANIC_FILE, OBS_SECRET
from nox.node.overlay import overlay_url
from nox.node.satellite_settings import SATELLITES_FILE, SettingsFile
from nox.node.voice import MUTE_FILE, PTT_FILE, STATE_FILE
from nox.security.model import SecretStore

__all__ = [
    "obs_password",
    "obs_resume",
    "overlay_line",
    "talk",
    "voice_mute",
    "voice_status",
]

#: The longest a push-to-talk from the terminal stays held.
TALK_MAX_S = 25.0


class SatelliteCliError(RuntimeError):
    """A message for the person at the terminal."""


def voice_mute(home: NodeHome, muted: bool) -> str:
    home.ensure()
    marker = home.path / MUTE_FILE
    if muted:
        marker.write_text("muted on this device\n", encoding="utf-8")
        return "Microphone muted on this device. Unmute here with: nox node voice unmute"
    marker.unlink(missing_ok=True)
    return "Microphone unmuted on this device."


def voice_status(home: NodeHome) -> list[str]:
    manifest = load_manifest(home.manifest_file)
    try:
        state: dict[str, Any] = json.loads((home.path / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    settings = SettingsFile(home.path / SATELLITES_FILE).read().voice
    on = bool(manifest.groups.get("voice"))
    lines = [
        f"Voice:       {'allowed on this device' if on else 'off (nox node allow voice)'}",
        f"Mode:        {settings.mode.replace('_', ' ')}",
        f"Muted here:  {'yes' if (home.path / MUTE_FILE).exists() else 'no'}",
        f"Running:     {'yes' if state.get('running') else 'no'}",
        f"Microphone:  {'open' if state.get('mic_open') else 'closed'}",
    ]
    if state and not state.get("core_allows"):
        lines.append(f"Core says:   no ({str(state.get('core_reason', ''))[:80]})")
    if state.get("error"):
        lines.append(f"Problem:     {str(state['error'])[:80]}")
    return lines


def talk(
    home: NodeHome,
    *,
    wait: Callable[[float], None],
    max_s: float = TALK_MAX_S,
) -> str:
    """Hold push-to-talk until `wait` returns (Enter) or `max_s` passed."""
    home.ensure()
    marker = home.path / PTT_FILE
    marker.write_text(f"{time.time()}\n", encoding="utf-8")
    try:
        wait(max_s)
    finally:
        marker.unlink(missing_ok=True)
    return "Released."


def obs_password(secrets: SecretStore | None, password: str | None, *, clear: bool = False) -> str:
    if secrets is None:
        raise SatelliteCliError("No keyring on this device: the OBS password cannot be stored.")
    if clear:
        secrets.delete(OBS_SECRET)
        return "OBS password removed from this device's keyring."
    if not password:
        raise SatelliteCliError("No password given; nothing was stored.")
    secrets.set(OBS_SECRET, password)
    return "OBS password stored in this device's keyring (it never goes to Nox's core)."


def obs_resume(home: NodeHome) -> str:
    """Lift the panic latch: OBS on this device takes changes again."""
    (home.path / OBS_PANIC_FILE).unlink(missing_ok=True)
    return "OBS takes changes from Nox again (the panic latch is lifted on this device)."


def overlay_line(home: NodeHome, *, variant: str = "") -> str:
    identity = home.load_identity()
    if identity is None:
        raise SatelliteCliError("This device is not paired yet: nox node pair <link>")
    manifest = load_manifest(home.manifest_file)
    port = SettingsFile(home.path / SATELLITES_FILE).read().overlay.port
    url = overlay_url(port, identity.node_id, variant=variant)
    note = "" if manifest.groups.get("obs") else "\n(Needs the group obs: nox node allow obs)"
    return f"{url}{note}"
