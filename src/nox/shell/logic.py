"""Pure shell logic (no Qt): tray tint, hotkey mapping, permission replies, kill path, shell model.

Implements the shell responsibilities from the Process Model and IPC Model request catalogue:
`privacy.set`, `voice.mute`, `voice.ptt`, `security.kill`, `security.permission.reply`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from nox.core.state import PrivacyMode, SystemLevel

# Hotkey combos as written in config ("ctrl+alt+space"). Modifier aliases normalised here.
_MODIFIER_ALIASES: dict[str, str] = {
    "control": "ctrl",
    "ctl": "ctrl",
    "strg": "ctrl",
    "option": "alt",
    "win": "cmd",
    "super": "cmd",
    "windows": "cmd",
    "return": "enter",
    "esc": "escape",
}

DEFAULT_HOTKEYS: dict[str, str] = {
    "ptt": "ctrl+alt+space",
    "mute": "ctrl+alt+m",
    "privacy": "ctrl+alt+p",
    "kill": "ctrl+alt+shift+k",
    "toggle_pet": "ctrl+alt+n",
}


class HotkeyAction(StrEnum):
    PTT = "ptt"
    MUTE = "mute"
    PRIVACY = "privacy"
    KILL = "kill"
    TOGGLE_PET = "toggle_pet"


class TrayTint(StrEnum):
    """Visual tray state. Ordered by priority; the shell maps each to a colour and tooltip."""

    OFFLINE = "offline"  # core not reachable
    SAFE_MODE = "safe_mode"  # kill switch engaged
    CAPTURING = "capturing"  # any of mic/camera/screen active
    CLOUD = "cloud"  # cloud request in flight, no local capture
    MUTED = "muted"
    PRIVATE = "private"  # privacy mode private/offline, no capture
    NORMAL = "normal"


TRAY_COLOURS: dict[TrayTint, str] = {
    TrayTint.OFFLINE: "#6b6b76",
    TrayTint.SAFE_MODE: "#e05a5a",
    TrayTint.CAPTURING: "#e0a23a",
    TrayTint.CLOUD: "#4ea1e0",
    TrayTint.MUTED: "#9a9aa8",
    TrayTint.PRIVATE: "#5fb37a",
    TrayTint.NORMAL: "#8b7cf6",
}


class CaptureFlags(BaseModel):
    microphone: bool = False
    camera: bool = False
    screen: bool = False
    cloud: bool = False

    @property
    def any_local(self) -> bool:
        return self.microphone or self.camera or self.screen


class ShellModel(BaseModel):
    """What the shell knows about the core. Updated only from IPC events and ping results."""

    connected: bool = False
    system_level: SystemLevel = SystemLevel.RUNNING
    privacy_mode: PrivacyMode = PrivacyMode.BALANCED
    previous_normal_privacy: PrivacyMode = PrivacyMode.BALANCED
    capture: CaptureFlags = Field(default_factory=CaptureFlags)
    muted: bool = False
    pet_visible: bool = True
    click_through: bool = False

    def apply_event(self, name: str, payload: dict[str, Any]) -> set[str]:
        """Apply an IPC event; return the set of changed field names."""
        before = self.model_dump()
        if name == "privacy.capture_changed":
            self.capture = CaptureFlags.model_validate(payload)
        elif name == "privacy.mode_changed":
            self.set_privacy(PrivacyMode(payload["current"]))
        elif name == "voice.muted":
            self.muted = bool(payload.get("muted", True))
        elif name == "security.kill_switch":
            self.system_level = SystemLevel.SAFE_MODE
        elif name == "system.started":
            self.system_level = SystemLevel.RUNNING
        elif name == "system.stopping":
            self.system_level = SystemLevel.STOPPING
        elif name == "state.changed":
            self._apply_state_change(payload)
        after = self.model_dump()
        return {k for k in after if after[k] != before[k]}

    def _apply_state_change(self, payload: dict[str, Any]) -> None:
        path = str(payload.get("path", ""))
        new = payload.get("new")
        if path == "assistant.muted":
            self.muted = bool(new)
        elif path == "privacy.mode" and isinstance(new, str):
            self.set_privacy(PrivacyMode(new))
        elif path == "system.level" and isinstance(new, str):
            self.system_level = SystemLevel(new)
        elif path.startswith("privacy.") and path.split(".")[1] in CaptureFlags.model_fields:
            setattr(self.capture, path.split(".")[1], bool(new))

    def set_privacy(self, mode: PrivacyMode) -> None:
        if self.privacy_mode in (PrivacyMode.FULL, PrivacyMode.BALANCED):
            self.previous_normal_privacy = self.privacy_mode
        self.privacy_mode = mode

    def set_connected(self, connected: bool) -> bool:
        """Return True when the value changed."""
        changed = connected != self.connected
        self.connected = connected
        if not connected:
            # Never claim capture is running when nobody can tell us.
            self.capture = CaptureFlags()
        return changed


def tray_tint(model: ShellModel) -> TrayTint:
    if not model.connected:
        return TrayTint.OFFLINE
    if model.system_level == SystemLevel.SAFE_MODE:
        return TrayTint.SAFE_MODE
    if model.capture.any_local:
        return TrayTint.CAPTURING
    if model.capture.cloud:
        return TrayTint.CLOUD
    if model.muted:
        return TrayTint.MUTED
    if model.privacy_mode in (PrivacyMode.PRIVATE, PrivacyMode.OFFLINE):
        return TrayTint.PRIVATE
    return TrayTint.NORMAL


def tray_tooltip(model: ShellModel, language: str = "de") -> str:
    de = language.startswith("de")
    if not model.connected:
        return "Nox – Kern nicht erreichbar" if de else "Nox – core unreachable"
    parts = [f"Nox – {model.privacy_mode.value}"]
    if model.system_level == SystemLevel.SAFE_MODE:
        parts.append("SAFE MODE")
    caps = [n for n in ("microphone", "camera", "screen", "cloud") if getattr(model.capture, n)]
    if caps:
        parts.append(("Aufnahme: " if de else "capture: ") + ", ".join(caps))
    if model.muted:
        parts.append("stumm" if de else "muted")
    return " | ".join(parts)


def parse_hotkey(combo: str) -> frozenset[str]:
    """'Ctrl+Alt+Space' -> frozenset({'ctrl', 'alt', 'space'}). Raises ValueError when empty."""
    keys: set[str] = set()
    for raw in combo.replace(" ", "").lower().split("+"):
        if not raw:
            continue
        keys.add(_MODIFIER_ALIASES.get(raw, raw))
    if not keys:
        raise ValueError(f"empty hotkey combo: {combo!r}")
    return frozenset(keys)


def hotkey_map(config: dict[str, Any] | None = None) -> dict[frozenset[str], HotkeyAction]:
    """Build combo -> action from the config tree.

    Sources: voice.stt.push_to_talk_hotkey, supervisor.kill_switch_hotkey, optional
    shell.hotkeys.*; everything else falls back to DEFAULT_HOTKEYS.
    """
    cfg = config or {}
    combos = dict(DEFAULT_HOTKEYS)
    ptt = cfg.get("voice", {}).get("stt", {}).get("push_to_talk_hotkey")
    if ptt:
        combos["ptt"] = str(ptt)
    kill = cfg.get("supervisor", {}).get("kill_switch_hotkey")
    if kill:
        combos["kill"] = str(kill)
    for key, value in cfg.get("shell", {}).get("hotkeys", {}).items():
        if key in combos and value:
            combos[key] = str(value)
    mapping: dict[frozenset[str], HotkeyAction] = {}
    for action_name, combo in combos.items():
        parsed = parse_hotkey(combo)
        if parsed in mapping:
            raise ValueError(
                f"hotkey conflict: {combo!r} bound to {mapping[parsed]} and {action_name}"
            )
        mapping[parsed] = HotkeyAction(action_name)
    return mapping


def permission_reply(request: dict[str, Any], *, allow: bool, remember: bool) -> dict[str, Any]:
    """Payload for `security.permission.reply` from a PermissionRequested payload + decision."""
    grant_id = request.get("request_id") or request.get("grant_id")
    if not grant_id:
        raise ValueError("permission request without request_id")
    decision = "allow" if allow else "deny"
    return {"grant_id": str(grant_id), "decision": decision, "remember": remember}


def kill_path(model: ShellModel) -> str:
    """'core' when the core answers pings, otherwise 'supervisor' (second path, Process Model)."""
    return "core" if model.connected else "supervisor"


def toggle_privacy(model: ShellModel) -> PrivacyMode:
    """Hotkey semantics (D235): jump to PRIVATE, or back to the last normal mode."""
    if model.privacy_mode in (PrivacyMode.PRIVATE, PrivacyMode.OFFLINE):
        return model.previous_normal_privacy
    return PrivacyMode.PRIVATE


def dashboard_url(http_port: int, token: str, host: str = "127.0.0.1") -> str:
    """Token in the fragment: fragments never reach HTTP access logs or Referer headers."""
    return f"http://{host}:{http_port}/dashboard/#token={token}"


def pet_url(
    http_port: int,
    token: str,
    host: str = "127.0.0.1",
    *,
    overlay: bool = False,
    variant: str = "neutral",
) -> str:
    """Token in the fragment (see dashboard_url); `overlay=1` and `variant=<id>` (OP-1, from
    `config.pet.variant`) are plain query flags — neither is a secret. `variant="neutral"` (the
    default) is omitted from the URL so existing/neutral setups produce the same URL as before.
    """
    params = []
    if overlay:
        params.append("overlay=1")
    if variant and variant != "neutral":
        params.append(f"variant={variant}")
    query = "?" + "&".join(params) if params else ""
    return f"http://{host}:{http_port}/pet/{query}#token={token}"


def ws_url(ws_port: int, host: str = "127.0.0.1") -> str:
    return f"ws://{host}:{ws_port}/ws"
