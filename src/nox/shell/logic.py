"""Pure shell logic (no Qt): tray tint, hotkey mapping, permission replies, kill path, shell model.

Implements the shell responsibilities from the Process Model and IPC Model request catalogue:
`privacy.set`, `voice.mute`, `voice.ptt`, `security.kill`, `security.permission.reply`.
"""

from __future__ import annotations

from collections.abc import Mapping
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

    def apply_status(self, status: Mapping[str, Any]) -> set[str]:
        """Apply a `privacy.status` snapshot: the level-triggered start after (re)connecting.

        Events only carry changes, so without this the tray showed its defaults - balanced,
        unmuted, not capturing, running - until the next change happened to arrive.
        """
        before = self.model_dump()
        mode = status.get("mode")
        if isinstance(mode, str) and mode in PrivacyMode.__members__.values():
            self.set_privacy(PrivacyMode(mode))
        capture = status.get("capture")
        if isinstance(capture, Mapping):
            self.capture = CaptureFlags.model_validate(dict(capture))
        if "muted" in status:
            self.muted = bool(status["muted"])
        # Only an answer that says so changes the level: a reply without `safe_mode` must not
        # lift a safe mode another snapshot (`state.get`) has just reported.
        if "safe_mode" in status:
            if status["safe_mode"]:
                self.system_level = SystemLevel.SAFE_MODE
            elif self.system_level is SystemLevel.SAFE_MODE:
                self.system_level = SystemLevel.RUNNING
        after = self.model_dump()
        return {k for k in after if after[k] != before[k]}

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


def hotkey_map_or_defaults(
    config: dict[str, Any] | None,
) -> tuple[dict[frozenset[str], HotkeyAction], str]:
    """`hotkey_map`, or the built-in combos plus the reason when the configured ones conflict.

    Two actions on one combo used to raise inside the shell's constructor, which put the shell
    into a crash loop under the supervisor; the user now gets working defaults and a message.
    """
    try:
        return hotkey_map(config), ""
    except ValueError as exc:
        return hotkey_map(None), str(exc)


#: `voice.ptt_refused` reasons as the user reads them, `(de, en)`.
PTT_REFUSAL_TEXT: dict[str, tuple[str, str]] = {
    "privacy_zone": (
        "Mikrofon bleibt aus: Datenschutzzone „{zone}“ ist aktiv.",
        "Microphone stays off: privacy zone “{zone}” is active.",
    ),
    "microphone_off": (
        "Mikrofon ist in den Privatsphäre-Einstellungen ausgeschaltet.",
        "The microphone is switched off in the privacy settings.",
    ),
    "muted": ("Nox ist stummgeschaltet.", "Nox is muted."),
    "panic": ("Panik-Modus: alle Aufnahmen sind aus.", "Panic mode: all capture is off."),
    "safe_mode": ("Not-Aus ist aktiv - Nox hört nicht zu.", "Kill switch engaged - not listening."),
    "voice_unavailable": (
        "Die Spracherkennung läuft gerade nicht.",
        "Speech recognition is not running right now.",
    ),
    "microphone_unavailable": (
        "Das Mikrofon lässt sich nicht öffnen.",
        "The microphone cannot be opened.",
    ),
}
_PTT_REFUSAL_FALLBACK = ("Das Mikrofon ist gerade geschlossen.", "The microphone is closed.")


def ptt_refusal_text(payload: Mapping[str, Any], language: str = "de") -> str:
    """Why push-to-talk did nothing, in one sentence the user can act on."""
    de, en = PTT_REFUSAL_TEXT.get(str(payload.get("reason", "")), _PTT_REFUSAL_FALLBACK)
    text = de if language.startswith("de") else en
    prefix = "Push-to-Talk: " if language.startswith("de") else "Push-to-talk: "
    return prefix + text.format(zone=str(payload.get("zone") or "?"))


#: Settings whose change means the hotkey listener must be rebuilt.
HOTKEY_SETTINGS = frozenset({"voice.stt.push_to_talk_hotkey", "supervisor.kill_switch_hotkey"})


#: `(x, y, width, height)` of a screen's usable area, in Qt's virtual desktop coordinates.
Rect = tuple[int, int, int, int]
#: At least this much of the pet must be on a screen for a saved position to count as visible.
PET_MIN_VISIBLE_PX = 48
PET_SCREEN_MARGIN_PX = 24


def pet_position(
    saved: tuple[int, int] | None, size: tuple[int, int], screens: list[Rect], primary: Rect
) -> tuple[int, int]:
    """Where the pet window opens: the saved position when it is on a screen that still exists,
    otherwise the bottom-right corner of the primary screen.

    Restoring blindly put the pet off-screen after undocking a laptop or unplugging a monitor,
    and nothing in the tray could bring it back.
    """
    width, height = size
    if saved is not None:
        x, y = saved
        for sx, sy, sw, sh in screens:
            overlap_w = min(x + width, sx + sw) - max(x, sx)
            overlap_h = min(y + height, sy + sh) - max(y, sy)
            if overlap_w >= PET_MIN_VISIBLE_PX and overlap_h >= PET_MIN_VISIBLE_PX:
                return x, y
    px, py, pw, ph = primary
    return (
        max(px, px + pw - width - PET_SCREEN_MARGIN_PX),
        max(py, py + ph - height - PET_SCREEN_MARGIN_PX),
    )


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
    """Token in the fragment (see dashboard_url); `overlay=1` and `variant=<id>` (from
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


#: What the tray says after "Fortsetzen", by the refusal reason `security.resume` or `sup.resume`
#: gave. German, like every other tray notice; the dashboard has the full, translated wording.
RESUME_NOTICES: dict[str, str] = {
    "ok": "Nox läuft wieder",
    "pin_required": "Fortsetzen braucht die PIN - bitte im Dashboard fortsetzen",
    "pin_wrong": "PIN falsch - bitte im Dashboard fortsetzen",
    "locked": "PIN gesperrt - später im Dashboard fortsetzen",
    "invalid_entry": "PIN-Eintrag ungültig - mit `nox pin set` neu setzen",
    "not_in_safe_mode": "Nox ist nicht im Sicherheitsmodus",
    "core_running": "Kern läuft - bitte im Dashboard fortsetzen",
}
RESUME_NOTICE_FALLBACK = "Fortsetzen nicht möglich"


def resume_notice(payload: dict[str, Any]) -> str:
    """The tray line for a `security.resume` / `sup.resume` answer."""
    if payload.get("ok") is True:
        return RESUME_NOTICES["ok"]
    reason = str(payload.get("reason") or "")
    return RESUME_NOTICES.get(reason, f"{RESUME_NOTICE_FALLBACK}: {reason or 'abgelehnt'}")
