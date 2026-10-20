"""What the desktop pet and the stream overlay are told: an allow-list, field by field.

Both pages only animate a creature, so they get exactly what the pet reducer
(`ui/pet/src/petState.ts`) reads, and nothing else - no spoken or transcribed text, no private
state, no setting other than the pet's own. Every event not listed is dropped; a listed event
loses every field not named. The stream overlay is stricter still: its credential sits in OBS's
configuration (scene exports, screenshots of the properties dialog), so it gets nothing about
speech meant only for the headphones, and nothing at all but the privacy mode itself while the
privacy mode is PRIVATE or OFFLINE.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The state roots both pages may see (also what `state.get` answers them with).
PUBLIC_STATE_ROOTS: tuple[str, ...] = ("assistant", "privacy", "system")
#: Inside those roots, the paths that carry words, not states: never shown to either page.
HIDDEN_STATE_PATHS: frozenset[str] = frozenset(
    {"assistant.current_task", "assistant.current_thought"}
)

ROLES: frozenset[str] = frozenset({"pet", "overlay"})
#: Privacy modes in which the overlay is told nothing but the mode itself.
_QUIET_MODES: frozenset[str] = frozenset({"private", "offline"})

_FIELDS: Mapping[str, frozenset[str]] = {
    "tts.started": frozenset({"utterance_id", "channel"}),
    "tts.finished": frozenset({"utterance_id", "ok", "channel"}),
    "tts.interrupted": frozenset({"utterance_id", "channel"}),
    "privacy.capture_changed": frozenset({"microphone", "camera", "screen", "cloud"}),
    "privacy.mode_changed": frozenset({"current", "previous"}),
    "voice.muted": frozenset({"muted"}),
    "system.stopping": frozenset(),
}


def _pick(payload: Mapping[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k in fields}


def public_state_path(path: str) -> bool:
    return path.split(".", 1)[0] in PUBLIC_STATE_ROOTS and not any(
        path == hidden or path.startswith(hidden + ".") for hidden in HIDDEN_STATE_PATHS
    )


#: What the overlay may know while privacy is PRIVATE or OFFLINE: the mode, nothing else.
QUIET_STATE_PATHS: frozenset[str] = frozenset({"privacy.mode"})


def _quiet(role: str, privacy_mode: str) -> bool:
    return role == "overlay" and privacy_mode in _QUIET_MODES


def public_snapshot(
    snapshot: Mapping[str, Any], role: str = "pet", privacy_mode: str = ""
) -> dict[str, Any]:
    """The `state.get` answer for the pet and the overlay - the same rules as the event view, so
    a page cannot learn by asking what it is not told."""
    if _quiet(role, privacy_mode):
        privacy = snapshot.get("privacy") or {}
        return {"privacy": {"mode": privacy.get("mode")}}
    public = {root: dict(snapshot.get(root) or {}) for root in PUBLIC_STATE_ROOTS}
    for hidden in HIDDEN_STATE_PATHS:
        root, _, leaf = hidden.partition(".")
        public.get(root, {}).pop(leaf, None)
    return public


def view(
    role: str, name: str, payload: Mapping[str, Any], *, privacy_mode: str = ""
) -> dict[str, Any] | None:
    """The payload `role` may receive for event `name`, or None when it gets nothing."""
    if role == "overlay":
        if _quiet(role, privacy_mode):
            quiet_ok = name == "privacy.mode_changed" or (
                name == "state.changed" and payload.get("path") in QUIET_STATE_PATHS
            )
            if not quiet_ok:
                return None
        if name.startswith("tts.") and payload.get("channel") == "private":
            return None
    if name.startswith("pet."):
        return dict(payload)
    if name == "tts.chunk":
        # The mouth follows how much is said, never what.
        text = payload.get("text")
        chars = len(text) if isinstance(text, str) else int(payload.get("chars", 0) or 0)
        return {"utterance_id": payload.get("utterance_id"), "chars": chars}
    if name == "state.changed":
        path = str(payload.get("path", ""))
        if not public_state_path(path):
            return None
        return _pick(payload, frozenset({"path", "old", "new", "version"}))
    if name == "settings.changed":
        paths = [p for p in payload.get("paths", []) if str(p).startswith("pet.")]
        return {"paths": paths} if paths else None
    fields = _FIELDS.get(name)
    return None if fields is None else _pick(payload, fields)
