"""Mouse and key combinations - the second and last file allowed to synthesise input.

`nox.desktop.keyboard` types letters and nothing else. This module adds what that one refuses: the
pointer (move, click, scroll) and key combinations (Ctrl+S, Alt+Tab). The owner approved this change
of the security model on 2026-10-18, and every line of what follows is a condition of it:

**Off unless switched on.** `desktop.computer_use.enabled` ships `false`, and turning it on is a
security setting: it needs the PIN when one is set.

**One confirmation per session, never per click and never forever.** A session is opened by
`computer.session_start`, which always asks the user (risk high). The confirmation grants the two
action tools for this session only - `session_minutes` and `max_actions_per_session` at most - and
ends with the session. A visible notice is shown while it runs, and every action is audited.

**Stop means stop.** The kill switch ends the session, and so does `stop_hotkey`
(Ctrl+Alt+Shift+K by default), read from the key state before every single event, not only
between actions.

**Never while a game runs.** The rule `keyboard.py` set holds here too, checked in this module and
not left to a caller: refused when a watched game is running anywhere, and when any game is in the
foreground. The check runs first, and again immediately before every event of an action, because a
game can start between two clicks. `game.input.send` stays a hard prohibition no profile lifts, and
the third-party automation libraries and the process-memory calls stay forbidden here as everywhere
(Security Model §10 names them; the CI scan cannot tell a mention from a call, so they are not
written out). The Win32 calls below are Nox's own.

**Not on itself.** Nox may not click into, move onto or send keys to its own windows - its
confirmation dialogs are in them, and a model that can click "Allow" has no permissions left.
Before *every* event - each button down and up, each wheel turn, each key down - the window under
the cursor (`WindowFromPoint(GetCursorPos())`) and the foreground window are checked against
Nox's windows: its processes, its Qt window classes, and a title naming Nox (which covers the
dashboard tab in a browser while it is the active tab). A move whose destination is a Nox window
is refused too, and nothing at all is sent while a Nox confirmation is waiting for an answer.

That check is a race by nature (a window can appear between the check and the event), so it is
not the guard that matters most. **A confirmation cannot be given by synthetic input:** while a
session is open and for a few seconds after Nox's last event, the core refuses an "allow" answer
and every privileged dashboard request (`blocks_confirmation`, wired in `nox.desktop.install`).
The user ends the session with the stop hotkey - a combination this module never sends - and then
answers. The combination blocklist (anything with Win, Ctrl+Esc, Alt+Tab, Alt+Esc, Alt+Space,
Ctrl+Alt+Del, Ctrl+Shift+Esc, the stop hotkey, Alt+F4 on Nox) only keeps the obvious ways of
handing control to something else out; it is not the boundary.

**Releasing keys respects the game rule too.** Keys that were pressed are released in a `finally`,
but not into a game: if a game came to the foreground or started meanwhile, the key-ups are
skipped and logged, and the session ends (the user's next physical key press clears the state).
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "BLOCKED_COMBOS",
    "ComputerUse",
    "ComputerUseError",
    "GameRunningError",
    "PointerAction",
    "foreground_pid",
    "parse_combo",
    "stop_hotkey_vks",
]

PointerAction = Literal["move", "click", "double_click", "right_click", "scroll"]

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
WHEEL_DELTA = 120
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

MODIFIERS: dict[str, int] = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
_ALIASES = {
    "control": "ctrl",
    "strg": "ctrl",
    "option": "alt",
    "windows": "win",
    "super": "win",
    "cmd": "win",
    "esc": "escape",
    "del": "delete",
    "entf": "delete",
    "return": "enter",
    "pgup": "pageup",
    "pgdn": "pagedown",
}
KEYS: dict[str, int] = {
    **{chr(c): c - 0x20 for c in range(ord("a"), ord("z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
    "enter": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}

#: Combinations that hand control to something Nox cannot see, or end the guard itself. Any
#: combination with the Windows key is refused as well (`combo_refusal`): Win+R/S/E/X/L/D/Tab and
#: Win+<number> all launch or switch something.
BLOCKED_COMBOS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"win", "r"}),
        frozenset({"win", "x"}),
        frozenset({"ctrl", "alt", "delete"}),
        frozenset({"ctrl", "shift", "escape"}),
        frozenset({"ctrl", "alt", "shift", "k"}),
        frozenset({"ctrl", "escape"}),
        frozenset({"alt", "tab"}),
        frozenset({"alt", "shift", "tab"}),
        frozenset({"ctrl", "alt", "tab"}),
        frozenset({"alt", "escape"}),
        frozenset({"alt", "space"}),
    }
)
#: Seconds after Nox's last synthetic event during which a confirmation is still refused.
CONFIRMATION_QUIET_S = 3.0
#: Refused when Nox itself is in front: closing it would close the dialogs that guard it.
BLOCKED_ON_NOX: frozenset[frozenset[str]] = frozenset({frozenset({"alt", "f4"})})
MAX_KEYS = 4


class ComputerUseError(Exception):
    """Nothing was sent; the message says why."""


class GameRunningError(ComputerUseError):
    """A game is running or in front. Nothing is sent anywhere while that is true."""


def parse_combo(combo: str) -> tuple[list[str], str]:
    """`"Ctrl+Shift+S"` -> (["ctrl", "shift"], "s"). Raises `ComputerUseError` for anything else."""
    parts = [_ALIASES.get(p.strip().lower(), p.strip().lower()) for p in combo.split("+")]
    if not parts or any(not p for p in parts) or len(parts) > MAX_KEYS:
        raise ComputerUseError(f"not a key combination: {combo!r}")
    *mods, key = parts
    for mod in mods:
        if mod not in MODIFIERS:
            raise ComputerUseError(f"{mod!r} is not a modifier (ctrl, alt, shift, win)")
    if len(set(mods)) != len(mods):
        raise ComputerUseError(f"a modifier appears twice in {combo!r}")
    if key not in KEYS and key not in MODIFIERS:
        raise ComputerUseError(f"unknown key {key!r}")
    return mods, key


def combo_refusal(mods: Sequence[str], key: str, *, nox_in_front: bool) -> str | None:
    pressed = frozenset([*mods, key])
    if pressed in BLOCKED_COMBOS or "win" in pressed:
        return f"{'+'.join([*mods, key])} is never sent"
    if nox_in_front and pressed in BLOCKED_ON_NOX:
        return "Nox is in front; it does not close itself"
    return None


# ---- the Win32 half ------------------------------------------------------------------------------


class Sender(Protocol):
    """What actually sends. Tests use a recorder; the real one is `Win32Sender`."""

    def screen(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) of the virtual desktop."""

    def window_at(self, x: int, y: int) -> tuple[Any, ...]:
        """(process id, title[, window class]) of the top-level window at a point."""

    def foreground(self) -> tuple[Any, ...]:
        """(process id, title[, window class]) of the foreground window."""

    def cursor(self) -> tuple[int, int]:
        """Where the pointer is now (`GetCursorPos`)."""

    def stop_pressed(self) -> bool: ...
    def mouse(self, dx: int, dy: int, flags: int, data: int = 0) -> None: ...
    def key(self, vk: int, *, up: bool) -> None: ...


if sys.platform == "win32":

    class _MouseInput(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        )

    class _KeybdInput(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        )

    class _Union(ctypes.Union):
        _fields_ = (("mi", _MouseInput), ("ki", _KeybdInput))

    class _Input(ctypes.Structure):
        _fields_ = (("type", wintypes.DWORD), ("union", _Union))


class Win32Sender:
    """`SendInput` for one event at a time, plus the reads the guards need."""

    def __init__(self, stop_vks: Sequence[int]) -> None:
        if sys.platform != "win32" or not hasattr(ctypes, "windll"):
            raise ComputerUseError("computer use is only available on Windows")
        self._user32 = ctypes.windll.user32
        self._stop_vks = tuple(stop_vks)

    def screen(self) -> tuple[int, int, int, int]:
        metric = self._user32.GetSystemMetrics
        return (
            int(metric(SM_XVIRTUALSCREEN)),
            int(metric(SM_YVIRTUALSCREEN)),
            int(metric(SM_CXVIRTUALSCREEN)),
            int(metric(SM_CYVIRTUALSCREEN)),
        )

    def _describe(self, handle: int) -> tuple[int, str, str]:
        if not handle:
            return 0, "", ""
        root = int(self._user32.GetAncestor(handle, 2)) or handle  # GA_ROOT
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        buffer = ctypes.create_unicode_buffer(512)
        self._user32.GetWindowTextW(root, buffer, 512)
        klass = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(root, klass, 256)
        return int(pid.value), buffer.value, klass.value

    def window_at(self, x: int, y: int) -> tuple[int, str, str]:
        return self._describe(int(self._user32.WindowFromPoint(wintypes.POINT(x, y))))

    def foreground(self) -> tuple[int, str, str]:
        return self._describe(int(self._user32.GetForegroundWindow()))

    def cursor(self) -> tuple[int, int]:
        point = wintypes.POINT()
        self._user32.GetCursorPos(ctypes.byref(point))
        return int(point.x), int(point.y)

    def stop_pressed(self) -> bool:
        state = self._user32.GetAsyncKeyState
        return bool(self._stop_vks) and all(int(state(vk)) & 0x8000 for vk in self._stop_vks)

    def _send(self, event: Any) -> None:
        sent = int(self._user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)))
        if sent != 1:
            raise ComputerUseError("Windows did not accept the input event")

    def mouse(self, dx: int, dy: int, flags: int, data: int = 0) -> None:
        mi = _MouseInput(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0)
        self._send(_Input(type=INPUT_MOUSE, union=_Union(mi=mi)))

    def key(self, vk: int, *, up: bool) -> None:
        ki = _KeybdInput(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0)
        self._send(_Input(type=INPUT_KEYBOARD, union=_Union(ki=ki)))


class DryRunSender:
    """Sends nothing: for the benchmark world (`NOX_COMPUTER_USE_DRY_RUN`). It can only make this
    module do less - every guard runs exactly as with the real sender."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int, int, int]] = []

    def screen(self) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1080)

    def window_at(self, x: int, y: int) -> tuple[int, str]:
        return (0, "")

    def foreground(self) -> tuple[int, str]:
        return (0, "")

    def cursor(self) -> tuple[int, int]:
        return (0, 0)

    def stop_pressed(self) -> bool:
        return False

    def mouse(self, dx: int, dy: int, flags: int, data: int = 0) -> None:
        self.events.append(("mouse", dx, dy, flags))

    def key(self, vk: int, *, up: bool) -> None:
        self.events.append(("key", vk, int(up), 0))


# ---- the guarded controller ----------------------------------------------------------------------


@dataclass
class Session:
    started: float
    until: float
    actions_left: int


Audit = Callable[[str, str, str], None]
Notice = Callable[[bool, str], None]


class ComputerUse:
    """Sessions and actions; every guard named in the module docstring lives here."""

    def __init__(
        self,
        *,
        enabled: Callable[[], bool],
        game_running: Callable[[], bool],
        game_in_foreground: Callable[[], bool],
        nox_pids: Callable[[], set[int]],
        sender: Callable[[], Sender],
        audit: Audit,
        notice: Notice = lambda _active, _reason: None,
        clock: Callable[[], float] = time.monotonic,
        nox_process: Callable[[int], bool] | None = None,
        confirmation_pending: Callable[[], bool] = lambda: False,
    ) -> None:
        self._enabled = enabled
        self._game_running = game_running
        self._game_in_foreground = game_in_foreground
        self._nox_pids = nox_pids
        self._make_sender = sender
        self._sender: Sender | None = None
        self._audit = audit
        self._notice = notice
        self._clock = clock
        self._session: Session | None = None
        #: Whether a process id is one of Nox's (core, workers, shell) - by process, not title.
        self._nox_process = nox_process or (lambda pid: pid in self._nox_pids())
        self._confirmation_pending = confirmation_pending
        self._last_event: float | None = None

    # -- sessions ------------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        session = self._session
        return session is not None and self._clock() < session.until and session.actions_left > 0

    def start_session(self, *, minutes: int, max_actions: int) -> Session:
        game_running = self._game_running()
        if game_running:
            raise GameRunningError("a game is running; Nox does not use the computer while it does")
        if not self._enabled():
            raise ComputerUseError(
                "computer use is switched off; it can be turned on in the settings (with the PIN)"
            )
        now = self._clock()
        self._session = Session(started=now, until=now + minutes * 60.0, actions_left=max_actions)
        self._audit("session.start", "", "ok")
        self._notice(True, f"for {minutes} minutes")
        log.warning("desktop.computer_use_started", minutes=minutes, max_actions=max_actions)
        return self._session

    def end_session(self, reason: str) -> bool:
        if self._session is None:
            return False
        self._session = None
        self._audit("session.end", reason, "ok")
        self._notice(False, reason)
        log.warning("desktop.computer_use_ended", reason=reason)
        return True

    # -- guards --------------------------------------------------------------------------------

    def _guard(self) -> Sender:
        """Run before every single event. The game check comes first and depends on nothing."""
        game_running = self._game_running()
        if game_running:
            self.end_session("a game started")
            raise GameRunningError("a game is running; Nox does not send input anywhere")
        if self._game_in_foreground():
            raise GameRunningError("a game is in the foreground; nothing was sent")
        if not self._enabled():
            self.end_session("switched off")
            raise ComputerUseError("computer use is switched off")
        if not self.active:
            self.end_session("session over")
            raise ComputerUseError(
                "no computer-use session is open; start one with computer.session_start"
            )
        if self._sender is None:
            self._sender = self._make_sender()
        if self._sender.stop_pressed():
            self.end_session("stop hotkey")
            raise ComputerUseError("stopped by the user's hotkey")
        return self._sender

    def blocks_confirmation(self) -> bool:
        """True while a session is open and for `CONFIRMATION_QUIET_S` after Nox's last event:
        the core then refuses an "allow" answer and privileged dashboard requests, so no click
        Nox synthesised can count as the user's decision."""
        if self.active:
            return True
        last = self._last_event
        return last is not None and self._clock() - last < CONFIRMATION_QUIET_S

    def _is_nox(self, window: tuple[Any, ...]) -> bool:
        pid, title = int(window[0]), str(window[1])
        klass = str(window[2]) if len(window) > 2 else ""
        if pid and (pid in self._nox_pids() or self._nox_process(pid)):
            return True
        if "nox" in title.lower():
            return True  # Nox's dialogs, and the dashboard while it is the browser's active tab
        # A Qt window whose process could not be read is treated as Nox's shell.
        return klass.startswith("Qt") and not pid

    def _event_guard(self, *, pointer: bool) -> Sender:
        """Everything that must hold right before one single input event."""
        sender = self._guard()
        if self._confirmation_pending():
            raise ComputerUseError(
                "a Nox confirmation is waiting for your answer; nothing is sent until it is given"
            )
        if self._is_nox(sender.foreground()):
            raise ComputerUseError("a Nox window is in front; Nox does not click itself")
        if pointer and self._is_nox(sender.window_at(*sender.cursor())):
            raise ComputerUseError("the pointer is on a Nox window; Nox does not click itself")
        self._last_event = self._clock()
        return sender

    def _spend(self) -> None:
        assert self._session is not None
        self._session.actions_left -= 1

    # -- actions -------------------------------------------------------------------------------

    def pointer(
        self, action: PointerAction, x: int | None = None, y: int | None = None, amount: int = 0
    ) -> str:
        sender = self._guard()
        if x is not None and y is not None:
            left, top, width, height = sender.screen()
            if not (left <= x < left + width and top <= y < top + height):
                raise ComputerUseError(f"({x}, {y}) is outside the screen")
            if self._is_nox(sender.window_at(x, y)):
                # Not even a move: a pointer resting on a Nox dialog is one click from "Allow".
                raise ComputerUseError("that point is on a Nox window; Nox does not click itself")
            ax = round((x - left) * 65535 / max(1, width - 1))
            ay = round((y - top) * 65535 / max(1, height - 1))
            self._event_guard(pointer=False).mouse(
                ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
            )
        steps: list[tuple[int, int]] = {
            "move": [],
            "click": [(MOUSEEVENTF_LEFTDOWN, 0), (MOUSEEVENTF_LEFTUP, 0)],
            "double_click": [(MOUSEEVENTF_LEFTDOWN, 0), (MOUSEEVENTF_LEFTUP, 0)] * 2,
            "right_click": [(MOUSEEVENTF_RIGHTDOWN, 0), (MOUSEEVENTF_RIGHTUP, 0)],
            "scroll": [(MOUSEEVENTF_WHEEL, max(-10, min(10, amount)) * WHEEL_DELTA)],
        }[action]
        for flags, data in steps:
            # Before every down, up and wheel event: game, stop key, Nox under the cursor, Nox in
            # front, a confirmation waiting.
            self._event_guard(pointer=True).mouse(0, 0, flags, data)
        self._spend()
        target = "" if x is None else f"{x},{y}"
        self._audit(f"pointer.{action}", target, "ok")
        return action

    def keys(self, combo: str) -> str:
        mods, key = parse_combo(combo)
        sender = self._guard()
        nox_in_front = self._is_nox(sender.foreground())
        refusal = combo_refusal(mods, key, nox_in_front=nox_in_front)
        if refusal is not None:
            self._audit("keys", "+".join([*mods, key]), "refused")
            raise ComputerUseError(refusal)
        if nox_in_front:
            raise ComputerUseError("a Nox window is in front; Nox does not send keys to itself")
        vks = [MODIFIERS[m] for m in mods] + [MODIFIERS.get(key) or KEYS[key]]
        pressed: list[int] = []
        try:
            for vk in vks:
                self._event_guard(pointer=False).key(vk, up=False)
                pressed.append(vk)
        finally:
            self._release(pressed)
        self._spend()
        name = "+".join([*mods, key])
        self._audit("keys", name, "ok")
        return name

    def _release(self, pressed: list[int]) -> None:
        """Release what was pressed - but never into a game (the rule holds for key-ups too)."""
        if not pressed or self._sender is None:
            return
        game_running = self._game_running() or self._game_in_foreground()
        if game_running:
            log.warning("desktop.computer_use_keyups_skipped", keys=len(pressed))
            self.end_session("a game came to the foreground")
            return
        for vk in reversed(pressed):
            self._sender.key(vk, up=True)


def foreground_pid() -> int:
    """Process id of the foreground window (0 off Windows or when there is none)."""
    if sys.platform != "win32" or not hasattr(ctypes, "windll"):
        return 0
    user32 = ctypes.windll.user32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return int(pid.value)


def stop_hotkey_vks(hotkey: str) -> list[int]:
    """Virtual-key codes of the stop hotkey, for `Win32Sender`."""
    mods, key = parse_combo(hotkey)
    return [MODIFIERS[m] for m in mods] + [MODIFIERS.get(key) or KEYS[key]]
