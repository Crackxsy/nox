"""`nox-node voice | obs | overlay` - the satellites' own terminal commands (C9).

All of them act on this device only, as the logged-in user: the microphone's mute switch (only
this device unmutes), push-to-talk from a terminal, the OBS password in this device's keyring,
lifting the OBS panic latch, and the overlay URL for an OBS browser source here. Unmuting and
lifting the latch widen what Nox may do: both refuse while Nox controls mouse and keyboard here
(`nox.node.cli.session_refusal`). The words follow `nox.node.cli_text`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from nox.node.cli_text import command, t
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


def _refuse_during_session(home: NodeHome) -> None:
    from nox.node.cli import session_refusal  # noqa: PLC0415 - the heavy module, only here

    refusal = session_refusal(home)
    if refusal is not None:
        raise SatelliteCliError(refusal)


def voice_mute(home: NodeHome, muted: bool) -> str:
    home.ensure()
    marker = home.path / MUTE_FILE
    if muted:
        marker.write_text("muted on this device\n", encoding="utf-8")
        return t("voice_muted", unmute=command("voice unmute"))
    _refuse_during_session(home)
    marker.unlink(missing_ok=True)
    return t("voice_unmuted")


def voice_status(home: NodeHome) -> list[str]:
    manifest = load_manifest(home.manifest_file)
    try:
        state: dict[str, Any] = json.loads((home.path / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    settings = SettingsFile(home.path / SATELLITES_FILE).read().voice
    on = bool(manifest.groups.get("voice"))

    def yes_no(value: object) -> str:
        return t("yes") if value else t("no")

    lines = [
        t(
            "voice_line",
            value=t("voice_on") if on else t("voice_off", allow=command("allow voice")),
        ),
        t("voice_mode", value=settings.mode.replace("_", " ")),
        t("voice_muted_here", value=yes_no((home.path / MUTE_FILE).exists())),
        t("voice_running", value=yes_no(state.get("running"))),
        t("voice_mic", value=t("mic_open") if state.get("mic_open") else t("mic_closed")),
    ]
    if state and not state.get("core_allows"):
        lines.append(t("voice_core", value=str(state.get("core_reason", ""))[:80]))
    if state.get("error"):
        lines.append(t("voice_problem", value=str(state["error"])[:80]))
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
    return t("released")


def obs_password(secrets: SecretStore | None, password: str | None, *, clear: bool = False) -> str:
    if secrets is None:
        raise SatelliteCliError(t("obs_no_keyring"))
    if clear:
        secrets.delete(OBS_SECRET)
        return t("obs_cleared")
    if not password:
        raise SatelliteCliError(t("obs_no_password"))
    secrets.set(OBS_SECRET, password)
    return t("obs_stored")


def obs_resume(home: NodeHome) -> str:
    """Lift the panic latch: OBS on this device takes changes again - never while Nox controls
    mouse and keyboard here."""
    _refuse_during_session(home)
    (home.path / OBS_PANIC_FILE).unlink(missing_ok=True)
    return t("obs_resumed")


def overlay_line(home: NodeHome, *, variant: str = "") -> str:
    identity = home.load_identity()
    if identity is None:
        raise SatelliteCliError(t("not_paired", pair=command('pair "<link>"')))
    manifest = load_manifest(home.manifest_file)
    port = SettingsFile(home.path / SATELLITES_FILE).read().overlay.port
    url = overlay_url(port, identity.node_id, variant=variant)
    note = (
        ""
        if manifest.groups.get("obs")
        else "\n" + t("overlay_needs_obs", allow=command("allow obs"))
    )
    return f"{url}{note}"
