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
* **Computer use** - the node is the authority. It reports its truth about computer use
  (`SessionReport`: a generation that only grows, whether a session is open, its id) in its hello,
  every pong and every tool answer. The core keeps, per node, the highest generation it saw and
  derives its approval lock from it, monotonically: a report older than what it knows is ignored,
  one it cannot read changes nothing. The lock (`blocks`) holds while the node says a session is
  open (and, unreachable, until it says otherwise or the longest session time has passed since it
  last said so) or while a start is on its way. While it holds, **no paired device** may approve
  or send a privileged request - approvals come only from the core machine's own dashboard or a
  node's own dialog. A start needs a browser device linked to that node and is refused while the
  lock holds for it; a start without a readable answer sends `computer.session_end`. Stops,
  kills and disconnects never lift the lock themselves - only the node's report does. While a
  session is open, `computer.pointer` and `computer.keys` are granted on that node only
  (`device:<id>|session:computer/*`, medium at most), and the dashboard shows "Nox steuert gerade
  Maus und Tastatur auf ..." with a stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nox.core.logging import get_logger
from nox.node.protocol import SessionReport, render
from nox.node.registry import BLOB_RESULT_KEY, SESSION_RESULT_KEY, NodeEntry, NodeRegistry
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
        # Asked again after the device's answer: panic, safe mode or a stricter privacy mode
        # switched on meanwhile discards the picture unseen.
        late = capture_refusal(kind, privacy())
        if late is not None:
            del data
            return {"ok": False, "error": "capture.refused", "detail": late, "device": entry.label}
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


#: The longest a computer-use session may last anywhere (`ComputerUseConfig.session_minutes`):
#: a node that stops reporting keeps the lock for at most this long after its last word.
MAX_SESSION_MINUTES = 120
LINK_HINT = (
    "Erst im Dashboard unter Geräte festlegen, welcher Browser auf diesem PC läuft (node.link) - "
    "sonst kann Nox nicht ausschließen, dass er sich selbst etwas erlaubt."
)
#: Ends the session on a node (`computer.session_end`); its answer as `NodeRegistry.call` gives it.
EndOnNode = Callable[[NodeEntry], Awaitable[dict[str, Any]]]
#: Where the latest hello/pong reports come from: (device id, report) pairs.
Reports = Callable[[], Iterable[tuple[str, Any]]]


def read_report(raw: Any) -> SessionReport | None:
    """A node's report, or None when it cannot be read (then nothing changes: fail closed)."""
    if isinstance(raw, SessionReport):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return SessionReport.model_validate(raw)
    except ValueError:
        return None


@dataclass
class _Attempt:
    #: When the start went out, and the generation known then.
    since: float
    gen: int


@dataclass
class _Device:
    label: str
    #: The highest generation reported, and what that report said.
    gen: int = -1
    active: bool = False
    session_id: str = ""
    #: When a report last said "active" (monotonic): unreachable, the lock holds this long more.
    active_seen: float = 0.0
    attempt: _Attempt | None = None
    grant_id: str = ""
    #: What the dashboard was last told (`desktop.computer_use` for this device).
    shown: bool = False
    #: Since when the lock holds for this device without a break: it never holds longer than the
    #: longest session (`MAX_SESSION_MINUTES`), whatever a node keeps saying.
    locked_since: float | None = None


class NodeControl:
    """The approval lock for computer use on nodes, derived from the nodes' own reports."""

    def __init__(
        self,
        engine: Any,
        publish: Callable[[dict[str, Any]], None],
        *,
        end_on_node: EndOnNode | None = None,
        reports: Reports = tuple,
        labels: Callable[[str], str] = lambda device_id: device_id[:8],
        stop_hotkey: str = "ctrl+alt+shift+k",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._publish = publish
        self._end_on_node = end_on_node
        self._reports = reports
        self._labels = labels
        self._stop = stop_hotkey
        self._clock = clock
        self.devices: dict[str, _Device] = {}
        self._ending: set[asyncio.Task[None]] = set()

    # -- the reports -----------------------------------------------------------------------------

    def _device(self, device_id: str, label: str = "") -> _Device:
        device = self.devices.get(device_id)
        if device is None:
            device = self.devices[device_id] = _Device(label or self._labels(device_id))
        elif label:
            device.label = label
        return device

    def observe(self, device_id: str, raw: Any, *, outcome_of_attempt: bool = False) -> None:
        """One report from a node. An older generation than known is ignored; the same one
        updates whether a session is open (it may have run out); a newer one replaces it. A
        start in flight is settled only by a report that shows its outcome: a newer generation,
        or the answer to that very start (`outcome_of_attempt`)."""
        report = read_report(raw)
        if report is None:
            return
        device = self._device(device_id)
        if report.gen < device.gen:
            return
        device.gen, device.active, device.session_id = report.gen, report.active, report.id
        if report.active:
            device.active_seen = self._clock()
        attempt = device.attempt
        if attempt is not None and (outcome_of_attempt or report.gen > attempt.gen):
            device.attempt = None
        self._sync(device_id, device)

    def hello(self, device_id: str, raw: Any) -> None:
        """A new authenticated connection of this node: a new run starts a new generation epoch,
        so its hello report replaces what was known (a node ends its sessions with a
        connection). A hello without a readable report changes nothing (fail closed)."""
        if read_report(raw) is None:
            return
        device = self._device(device_id)
        device.gen, device.active, device.attempt = -1, False, None
        self.observe(device_id, raw)

    def release(self, reason: str) -> int:
        """The owner lifts the lock by hand (`node.computer.release`, with the PIN): for a node
        that went silent. A node that still reports a running session locks again with its next
        report. Returns how many devices were released."""
        released = 0
        for device_id, device in self.devices.items():
            if self._locked(device):
                released += 1
            device.active, device.attempt, device.locked_since = False, None, None
            self._sync(device_id, device, reason=reason)
        return released

    def status(self) -> list[dict[str, Any]]:
        """What the dashboard shows right now (on connect): one entry per locked device."""
        self.active()
        return [
            self._payload(device_id, device, "running")
            for device_id, device in self.devices.items()
            if device.shown
        ]

    def pull(self) -> None:
        """Read the latest hello/pong reports of connected nodes."""
        for device_id, raw in self._reports():
            self.observe(device_id, raw)

    # -- the lock --------------------------------------------------------------------------------

    def _open(self, device: _Device) -> bool:
        """The node says a session is open (and said so recently enough)."""
        limit = MAX_SESSION_MINUTES * 60.0
        return device.active and self._clock() - device.active_seen < limit

    def _held(self, device: _Device) -> bool:
        attempt = device.attempt
        in_flight = attempt is not None and self._clock() - attempt.since < MAX_SESSION_MINUTES * 60
        return self._open(device) or in_flight

    def _locked(self, device: _Device) -> bool:
        if not self._held(device):
            return False
        since = device.locked_since
        return since is None or self._clock() - since < MAX_SESSION_MINUTES * 60

    def locked(self, device_id: str) -> bool:
        self.pull()
        device = self.devices.get(device_id)
        return device is not None and self._locked(device)

    def active(self) -> bool:
        self.pull()
        for device_id, device in self.devices.items():
            self._sync(device_id, device)  # time may have passed
        return any(self._locked(device) for device in self.devices.values())

    def blocks(self, client_device: str | None) -> bool:
        """Whether a paired device's approval is refused right now: while the lock holds for
        any node, every paired device - none is provably on another machine. Approvals then come
        only from the core machine's own dashboard or a node's own dialog (spec C §1.5)."""
        return bool(client_device) and self.active()

    def _payload(self, device_id: str, device: _Device, reason: str) -> dict[str, Any]:
        return {
            "active": device.shown,
            "reason": reason,
            "stop": self._stop,
            "device": device.label,
            "device_id": device_id,
        }

    def _sync(self, device_id: str, device: _Device, *, reason: str = "") -> None:
        """Grant and notice follow the derived state, never an event."""
        held = self._held(device)
        if held and device.locked_since is None:
            device.locked_since = self._clock()
        elif not held:
            device.locked_since = None
        is_open = self._open(device) and self._locked(device)
        if is_open and not device.grant_id and self._engine is not None:
            device.grant_id = f"computer-{uuid.uuid4().hex[:12]}"
            remaining = MAX_SESSION_MINUTES * 60.0 - (self._clock() - device.active_seen)
            self._engine.grant_temporary(
                TemporaryGrant(
                    grant_id=device.grant_id,
                    scope=f"device:{device_id}|session:computer/*",
                    max_risk=Risk.MEDIUM,
                    expires_at=datetime.now(UTC) + timedelta(seconds=max(remaining, 1.0)),
                    granted_by="node-session",
                    label=f"computer use on {device.label}",
                )
            )
        elif not is_open and device.grant_id:
            if self._engine is not None:
                with contextlib.suppress(Exception):
                    self._engine.revoke(device.grant_id)
            device.grant_id = ""
        locked = self._locked(device)
        if locked != device.shown:
            device.shown = locked
            default = "running" if is_open else "starting or unclear" if locked else "over"
            self._publish(self._payload(device_id, device, reason or default))

    # -- the routes ------------------------------------------------------------------------------

    async def _start(self, entry: NodeEntry, call: NodeCall) -> dict[str, Any]:
        if not entry.linked_device_id:
            # Without knowing which browser sits on that PC, it could not be kept from approving.
            return {"ok": False, "error": "link_required", "device": entry.label, "hint": LINK_HINT}
        device = self._device(entry.device_id, entry.label)
        if self.locked(entry.device_id):
            # Never a second attempt over a running or unclear one.
            return {
                "ok": False,
                "error": "session_running",
                "device": entry.label,
                "hint": "end it first (computer.session_end) or stop",
            }
        device.attempt = _Attempt(self._clock(), device.gen)
        self._sync(entry.device_id, device)
        try:
            answer = await call()
        except BaseException:
            # Cancelled or failed: the attempt stays (fail closed), and the session is ended on
            # the node in the background - shielded, so the lock is not held for the longest
            # session only because the caller went away.
            self._background_end(entry)
            raise
        self.observe(entry.device_id, answer.get(SESSION_RESULT_KEY), outcome_of_attempt=True)
        if device.attempt is not None:
            # No readable report in the answer: end it there to be sure.
            await self._confirm_end(entry)
        return answer

    def _background_end(self, entry: NodeEntry) -> None:
        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(self._confirm_end(entry))
            self._ending.add(task)
            task.add_done_callback(self._ending.discard)

    async def _confirm_end(self, entry: NodeEntry) -> None:
        if self._end_on_node is None:
            return
        try:
            ended = await self._end_on_node(entry)
        except Exception as exc:  # noqa: BLE001 - no answer: the lock simply stays
            log.warning("node.computer_end_failed", error=type(exc).__name__)
            return
        result = ended.get("result")
        # Only an end that ran on the node (after the start) shows the start's outcome.
        ran = isinstance(result, dict) and result.get("ok") is True
        self.observe(entry.device_id, ended.get(SESSION_RESULT_KEY), outcome_of_attempt=ran)

    def around(self, tool: str) -> NodeAround:
        async def around(entry: NodeEntry, _args: dict[str, Any], call: NodeCall) -> dict[str, Any]:
            if tool == "computer.session_start":
                return await self._start(entry, call)
            answer = await call()
            result = answer.get("result")
            ran = (
                tool == "computer.session_end"
                and isinstance(result, dict)
                and result.get("ok") is True
            )
            self.observe(entry.device_id, answer.get(SESSION_RESULT_KEY), outcome_of_attempt=ran)
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


def private_now(privacy: Any) -> bool:
    """Panic or safe mode on this core (or no privacy settings to tell): titles are hidden."""
    if privacy is None:
        return True
    try:
        return bool(getattr(privacy, "panic", False)) or bool(privacy.snapshot().safe_mode)
    except Exception:  # noqa: BLE001 - cannot tell: hide
        return True


def _redact_around(privacy: Callable[[], Any]) -> NodeAround:
    """`desktop.windows` from a node: window titles hidden in panic and safe mode, like
    everything else that could show what is on a screen."""

    async def around(entry: NodeEntry, _args: dict[str, Any], call: NodeCall) -> dict[str, Any]:
        answer = await call()
        result = answer.get("result")
        if isinstance(result, dict) and private_now(privacy()):
            windows = result.get("windows")
            if isinstance(windows, list):
                result["windows"] = [
                    {**w, "title": "[hidden: privacy]"} if isinstance(w, dict) else w
                    for w in windows
                ]
        return answer

    return around


def _routes(
    control: NodeControl, captures: Callable[[str], NodeAround], privacy: Callable[[], Any]
) -> list[_Route]:
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
        _Route("desktop.windows", read, False, m["empty"], "The open windows.",
               around=_redact_around(privacy)),
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
    for route in _routes(control, captures, privacy):
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


# ---- approvals while Nox controls a node ---------------------------------------------------------

REFUSE_DEVICE = (
    "Nox controls mouse and keyboard on a paired PC; approve on the computer Nox runs on "
    "(with the PIN), or deny or stop"
)
REFUSE_REMOTE = (
    "Nox controls mouse and keyboard on a paired PC and this computer is used remotely (or that "
    "cannot be told); approve at the computer itself, or deny or stop"
)
REFUSE_NO_PIN = (
    "Nox controls mouse and keyboard on a paired PC; approving now needs the security PIN, and "
    "none is set - deny, stop, or set a PIN first"
)


async def local_with_pin(
    ctx: Any, pin: str | None, *, gate: Any, remote: Callable[[], bool | None], action: str
) -> str | None:
    """A local UI with the PIN at the machine itself - or why not."""
    from nox.ipc.roles import LOCAL_UI_ROLES  # noqa: PLC0415
    from nox.security.gate import PinRequiredError  # noqa: PLC0415

    if getattr(ctx, "role", "") not in LOCAL_UI_ROLES:
        return REFUSE_DEVICE
    if remote() is not False:
        return REFUSE_REMOTE
    if gate is None or not gate.is_required():
        return REFUSE_NO_PIN
    try:
        await gate.require(pin, action=action, by=getattr(ctx, "client_id", "") or ctx.role)
    except PinRequiredError as exc:
        return exc.reason
    return None


async def approval_refusal(
    control: NodeControl,
    ctx: Any,
    pin: str | None,
    *,
    gate: Any,
    remote: Callable[[], bool | None],
) -> str | None:
    """Why an "allow" is refused while the lock holds for any node, or None (spec C §1.5).

    Paired devices never approve then (none is provably on another machine). The core
    machine's own dashboard or shell may, but only with the security PIN and not from a remote
    session - a remote desktop could be opened from the very PC Nox controls. "Deny" and Stopp
    are never refused (the caller asks only for "allow")."""
    if not control.active():
        return None
    return await local_with_pin(
        ctx, pin, gate=gate, remote=remote, action="security.permission.reply"
    )
