"""The core's side of C6: `screen.capture`, `camera.snapshot`, `browser.*`, `desktop.*`,
`computer.*` and `media.control` routed to nodes (spec C §1.4/§1.5).

Each runs through the same path as every routed tool (`nox.node.router`): the device resolved in
the permission scope, the permission request naming it, a signed receipt per call, the node's own
gate, budget and audit. What this module adds:

* **Captures** need this core's privacy to allow them (FULL or BALANCED, the capture switch for
  that kind on - the camera's is off by default -, no panic or safe mode); they are high risk on a
  node, so the person at the device confirms every one there, and they are never remembered. The
  node sends the picture as `nox/blob` chunks; it is checked (PNG, size, SHA-256) and stored under
  this core's captures folder like a capture of its own, so `vision.describe` and its rules
  (camera pictures never to the cloud) apply unchanged. The model gets the path, never the image.
* **Computer use**: a confirmed `computer.session_start` on a node grants `computer.pointer` and
  `computer.keys` *on that node only* (`device:<id>|session:computer/*`, medium at most) for the
  session's minutes, and the dashboard shows "Nox steuert gerade Maus und Tastatur auf ..." with a
  stop. The session ends here when the node says so, on `computer.session_end`, the kill switch,
  the node going offline, or its time. While it runs, an "allow" from the browser device linked to
  that node is refused (`blocks`), as on the core machine itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nox.core.logging import get_logger
from nox.node.protocol import render
from nox.node.registry import BLOB_RESULT_KEY, NodeEntry, NodeRegistry
from nox.node.router import CannotShow, NodeAround, NodeCall, routed_spec
from nox.security.model import Risk, TemporaryGrant
from nox.tools.registry import ToolSpec

log = get_logger(__name__)

__all__ = ["NodeControl", "store_capture", "world_specs"]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
KEEP_CAPTURES = 50
#: Longest permission target; longer is refused, never cut (`nox.node.install`).
MAX_TARGET_CHARS = 1000
_SUFFIX = " On this core or on a paired PC (`device`)."


def _all_args(payload: dict[str, Any]) -> str:
    """Every argument, rendered like the device's own dialog - in full or not at all."""
    text = " ".join(f"{key} {render(value)}" for key, value in payload.items()) or "-"
    if len(text) > MAX_TARGET_CHARS:
        raise CannotShow(f"the call is too long to be shown in full ({len(text)} characters)")
    return text


# ---- captures ------------------------------------------------------------------------------------


def store_capture(root: Path, kind: str, png: bytes, *, keep: int = KEEP_CAPTURES) -> Path:
    """A node's picture under this core's captures folder, named like the core's own
    (`nox.hands.senses`), the oldest beyond `keep` removed."""
    folder = root / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{kind}-{int(time.time())}-{uuid.uuid4().hex[:8]}.png"
    path.write_bytes(png)
    files = sorted(folder.glob(f"{kind}-*.png"), key=lambda p: p.stat().st_mtime)
    for old in files[: max(len(files) - max(keep, 1), 0)]:
        with contextlib.suppress(OSError):
            old.unlink()
    return path


def capture_refusal(kind: str, privacy: Any) -> str | None:
    """Why this core does not ask a node for a picture of `kind` right now, or None."""
    if privacy is None:
        return "no privacy settings on this core"
    mode = getattr(privacy.mode, "value", str(privacy.mode))
    if mode not in ("full", "balanced"):
        return f"privacy mode {mode} does not capture"
    if not privacy.allows_capture(kind):
        return f"{kind} capture is off (privacy settings, panic or safe mode)"
    return None


def _capture_around(kind: str, root: Callable[[], Path], privacy: Callable[[], Any]) -> NodeAround:
    async def around(entry: NodeEntry, _args: dict[str, Any], call: NodeCall) -> dict[str, Any]:
        refusal = capture_refusal(kind, privacy())
        if refusal is not None:
            return {
                "ok": False,
                "error": "capture.refused",
                "detail": refusal,
                "device": entry.label,
            }
        answer = await call(blob=True)
        data = answer.pop(BLOB_RESULT_KEY, None)
        if "error" in answer:
            return answer
        meta = answer.get("result")
        if not isinstance(meta, dict) or meta.get("ok") is not True:
            shown = meta if isinstance(meta, dict) else {"ok": False, "error": "malformed_result"}
            return {**shown, "device": entry.label}
        if (
            not isinstance(data, bytes)
            or not data.startswith(PNG_MAGIC)
            or len(data) > MAX_CAPTURE_BYTES
            or hashlib.sha256(data).hexdigest() != meta.get("sha256")
        ):
            return {"ok": False, "error": "malformed_result", "device": entry.label}
        path = await asyncio.to_thread(store_capture, root(), kind, data)
        from nox.hands.senses import cloud_refusal  # noqa: PLC0415

        view = privacy()
        return {
            "ok": True,
            "path": str(path),
            "device": entry.label,
            "stays_local": True,
            "cloud_allowed": view is not None and cloud_refusal(kind, view) is None,
        }

    return around


# ---- computer use on a node ----------------------------------------------------------------------


@dataclass
class _Session:
    label: str
    until: float
    grant_id: str
    timer: asyncio.TimerHandle | None = None


class NodeControl:
    """Computer-use sessions running on nodes, as this core knows them."""

    def __init__(
        self,
        engine: Any,
        publish: Callable[[dict[str, Any]], None],
        *,
        linked: Callable[[str], str | None] = lambda _id: None,
        stop_hotkey: str = "ctrl+alt+shift+k",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._publish = publish
        self._linked = linked
        self._stop = stop_hotkey
        self._clock = clock
        self.sessions: dict[str, _Session] = {}

    def start(self, entry: NodeEntry, minutes: int) -> None:
        minutes = max(1, min(int(minutes), 120))
        self.end(entry.device_id, "a new session replaces it", quiet=True)
        grant_id = f"computer-{uuid.uuid4().hex[:12]}"
        if self._engine is not None:
            self._engine.grant_temporary(
                TemporaryGrant(
                    grant_id=grant_id,
                    scope=f"device:{entry.device_id}|session:computer/*",
                    max_risk=Risk.MEDIUM,
                    expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
                    granted_by="user",
                    label=f"computer use on {entry.label}",
                )
            )
        session = _Session(entry.label, self._clock() + minutes * 60.0, grant_id)
        with contextlib.suppress(RuntimeError):
            session.timer = asyncio.get_running_loop().call_later(
                minutes * 60.0, self.end, entry.device_id, "session over"
            )
        self.sessions[entry.device_id] = session
        self._notice(True, entry.device_id, session.label, f"for {minutes} minutes")

    def end(self, device_id: str, reason: str, *, quiet: bool = False) -> bool:
        session = self.sessions.pop(device_id, None)
        if session is None:
            return False
        if session.timer is not None:
            session.timer.cancel()
        if self._engine is not None:
            with contextlib.suppress(Exception):
                self._engine.revoke(session.grant_id)
        if not quiet:
            self._notice(False, device_id, session.label, reason)
        return True

    def end_all(self, reason: str) -> None:
        for device_id in list(self.sessions):
            self.end(device_id, reason)

    def blocks(self, client_device: str | None) -> bool:
        """Whether an "allow" from this paired browser device is refused: it sits on a node whose
        mouse and keyboard Nox controls right now (spec C §1.5)."""
        if not client_device:
            return False
        now = self._clock()
        return any(
            self._linked(device_id) == client_device
            for device_id, session in self.sessions.items()
            if session.until > now
        )

    def _notice(self, active: bool, device_id: str, label: str, reason: str) -> None:
        self._publish(
            {
                "active": active,
                "reason": reason,
                "stop": self._stop,
                "device": label,
                "device_id": device_id,
            }
        )

    def around(self, tool: str) -> NodeAround:
        async def around(entry: NodeEntry, _args: dict[str, Any], call: NodeCall) -> dict[str, Any]:
            answer = await call()
            result = answer.get("result")
            ok = isinstance(result, dict) and result.get("ok") is True
            if tool == "computer.session_start":
                if ok:
                    assert isinstance(result, dict)
                    minutes = result.get("minutes")
                    self.start(entry, minutes if isinstance(minutes, int) else 15)
            elif tool == "computer.session_end":
                if ok or answer.get("error") == "refused_on_device":
                    self.end(entry.device_id, "ended by Nox")
            elif isinstance(result, dict) and result.get("session_active") is False:
                self.end(entry.device_id, "session over on the device")
            elif answer.get("error") in ("device_offline", "refused_on_device"):
                self.end(entry.device_id, "the device stopped it")
            return answer

        return around


# ---- the specs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Route:
    name: str
    risk: Risk
    side_effects: bool
    model: type[BaseModel]
    description: str
    target: Callable[[dict[str, Any]], str] = _all_args
    node_risk: Risk | None = None
    remember: bool = True
    around: NodeAround | None = None


def _models() -> dict[str, type[BaseModel]]:
    from nox.desktop.computer_tools import KeysInput, PointerInput, SessionStartInput  # noqa: PLC0415
    from nox.desktop.tools import Empty, ProcessRef, TypeInput, WindowRef  # noqa: PLC0415
    from nox.hands.browser import ClickInput, OpenInput, ReadInput  # noqa: PLC0415
    from nox.hands.browser import TypeInput as BrowserType  # noqa: PLC0415
    from nox.node.world import MediaArgs, NoArgs  # noqa: PLC0415

    return {
        "none": NoArgs,
        "empty": Empty,
        "window": WindowRef,
        "process": ProcessRef,
        "type": TypeInput,
        "start": SessionStartInput,
        "pointer": PointerInput,
        "keys": KeysInput,
        "open": OpenInput,
        "read": ReadInput,
        "click": ClickInput,
        "btype": BrowserType,
        "media": MediaArgs,
    }


def _routes(control: NodeControl, captures: Callable[[str], NodeAround]) -> list[_Route]:
    m = _models()
    high, med, low, read = Risk.HIGH, Risk.MEDIUM, Risk.LOW, Risk.READ
    window = (
        ("desktop.window_focus", low, "Bring one window to the front"),
        ("desktop.window_minimize", low, "Minimise one window"),
        ("desktop.window_restore", low, "Restore one minimised window"),
        ("desktop.window_close", med, "Ask a program to close (it may still ask to save)"),
    )
    routes = [
        # Captures: high on a node - asked on the device every time, never remembered.
        _Route("screen.capture", med, True, m["none"], "Screenshot of a screen.",
               lambda _p: "screen", high, False, captures("screen")),
        _Route("camera.snapshot", med, True, m["none"], "One camera picture.",
               lambda _p: "camera", high, False, captures("camera")),
        _Route("browser.open", low, False, m["open"], "Open a public web page in Nox's browser."),
        _Route("browser.read", low, False, m["read"], "Text and links of the open page."),
        *(
            _Route(f"browser.{verb}", med, True, model, f"{verb.title()} on the open page.",
                   remember=False)
            for verb, model in (("click", m["click"]), ("type", m["btype"]), ("submit", m["click"]))
        ),
        _Route("desktop.windows", read, False, m["empty"], "The open windows."),
        _Route("desktop.processes", read, False, m["empty"], "Programs using the most memory."),
        *(_Route(name, risk, True, m["window"], what + ".") for name, risk, what in window),
        _Route("desktop.process_stop", high, True, m["process"],
               "End a program by its process id (unsaved work is lost).", remember=False),
        _Route("desktop.type_text", high, True, m["type"],
               "Type text into one window; never while a game runs.", remember=False),
        _Route("computer.session_start", high, True, m["start"],
               "Ask for a short session with mouse and keys; never in games.", remember=False,
               around=control.around("computer.session_start")),
        *(
            _Route(name, med, True, m[key], what, remember=False, around=control.around(name))
            for name, key, what in (
                ("computer.pointer", "pointer", "Inside a session: move, click or scroll."),
                ("computer.keys", "keys", "Inside a session: press one key or combination."),
            )
        ),
        _Route("computer.session_end", low, True, m["empty"], "End the computer-use session.",
               around=control.around("computer.session_end")),
        _Route("media.control", low, True, m["media"],
               "Music and sound on a paired PC: play, pause, next, previous, volume, mute."),
    ]  # fmt: skip
    return routes


def _fixed(risk: Risk) -> Callable[[dict[str, Any]], Risk]:
    return lambda _payload: risk


def world_specs(
    *,
    registry: NodeRegistry,
    own: Callable[[str], ToolSpec | None],
    defaults: Callable[[], Mapping[str, str]],
    control: NodeControl,
    captures_root: Callable[[], Path],
    privacy: Callable[[], Any],
) -> list[ToolSpec]:
    """The routable specs of C6, each the core's own tool when the call stays here."""

    def captures(kind: str) -> NodeAround:
        return _capture_around(kind, captures_root, privacy)

    specs: list[ToolSpec] = []
    for route in _routes(control, captures):
        mine = own(route.name)
        node_risk = route.node_risk
        specs.append(
            routed_spec(
                name=route.name,
                description=(mine.description if mine is not None else route.description) + _SUFFIX,
                risk=route.risk,
                side_effects=route.side_effects,
                registry=registry,
                core=mine,
                input_model=route.model,
                defaults=defaults,
                node_target=route.target,
                node_risk=_fixed(node_risk) if node_risk is not None else None,
                remember_on_node=route.remember,
                around=route.around,
            )
        )
    return specs
