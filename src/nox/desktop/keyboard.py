"""Typing into a window - the only file in the repository allowed to synthesise input.

Everything about this module is a boundary, so the reasoning is worth spelling out.

**Why it exists at all.** "Schreib das in den Editor" is a reasonable thing to ask an assistant that
can already see the window. Without input synthesis it cannot be done at all.

**Why it is one file.** The `guard-security-model` CI job greps the whole source tree for
input-synthesis APIs and fails the build when one appears. It now exempts exactly this path, and
gains a second check that this file still contains the game guard below. The process-memory calls
and the third-party automation libraries stay forbidden everywhere, this file included; they are
named in `docs/SECURITY.md`, which the scan does not read.

That scan cannot tell a mention from a call, which is why none of them is written out here: it
found this very docstring the first time it ran. Keeping the guard strict beats naming them.

**Why it refuses while a game is running.** The user asked for the prohibition to become
window-scoped, and it is: the tool refuses the game's own window. This module goes further and
refuses to send anything at all while a watched game process is running anywhere on the machine.
Rocket League's boundary is not only about where the input lands - a process that observes the game
*and* synthesises input is exactly the shape an anti-cheat is right to distrust, and the cost of
being wrong is the user's account, not a failed request. `game.input.send` also remains in the
immutable hard-prohibition list, which no profile or configuration can lift.

**Text, not shortcuts.** `KEYEVENTF_UNICODE` types characters, which is layout-independent and
cannot express Alt+F4 or Ctrl+A. That is the point: the damage in synthetic input is almost never in
the letters, it is in the combinations.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["GameRunningError", "TypingError", "available", "type_text"]

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D

#: One call's worth. A model asked to write a paragraph should write a paragraph, not a book, and an
#: unbounded loop of synthetic keystrokes is not something to leave to a language model.
MAX_CHARS = 2000


class TypingError(Exception):
    """The keystrokes were not sent. Nothing was typed."""


class GameRunningError(TypingError):
    """A watched game is running. Nothing is typed anywhere while that is true."""


if sys.platform == "win32":

    class _KeyboardInput(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        )

    class _InputUnion(ctypes.Union):
        _fields_ = (("ki", _KeyboardInput),)

    class _Input(ctypes.Structure):
        _fields_ = (("type", wintypes.DWORD), ("union", _InputUnion))


def available() -> bool:
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def _key_event(*, scan: int = 0, vk: int = 0, flags: int = 0) -> _Input:
    return _Input(
        type=INPUT_KEYBOARD,
        union=_InputUnion(
            ki=_KeyboardInput(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
        ),
    )


def _events_for(text: str, *, press_enter: bool) -> list[_Input]:
    """Down and up for every character, as Unicode scan codes rather than virtual keys."""
    events: list[_Input] = []
    for character in text:
        code = ord(character)
        events.append(_key_event(scan=code, flags=KEYEVENTF_UNICODE))
        events.append(_key_event(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    if press_enter:
        events.append(_key_event(vk=VK_RETURN))
        events.append(_key_event(vk=VK_RETURN, flags=KEYEVENTF_KEYUP))
    return events


def type_text(
    text: str,
    *,
    press_enter: bool = False,
    game_running: bool,
    expected_window: int,
) -> int:
    """Type `text` into whatever has focus, having checked that focus is `expected_window`.

    `game_running` is passed in rather than looked up here, so the caller's answer - which
    comes from the configured game list - is the one that counts, and this module cannot be
    fooled by its own guess. It is checked first, before anything else, because the answer "no"
    must not depend on getting the rest right.

    The foreground window is re-read immediately before sending. Focus moves for reasons
    nothing here controls, and text typed into whatever happens to be in front instead is the
    failure that would make this feature indefensible.
    """
    if game_running:
        raise GameRunningError(
            "a game is running; Nox does not send input anywhere while that is true"
        )
    if not available():  # pragma: no cover - the target platform is Windows
        raise TypingError("typing is only available on Windows")
    if not text:
        raise TypingError("there was nothing to type")
    if len(text) > MAX_CHARS:
        raise TypingError(f"that is {len(text)} characters; the limit is {MAX_CHARS}")

    user32 = ctypes.windll.user32
    in_front = int(user32.GetForegroundWindow())
    if in_front != int(expected_window):
        raise TypingError(
            "the window moved out of the foreground before anything was typed; nothing was sent"
        )

    events = _events_for(text, press_enter=press_enter)
    array = (_Input * len(events))(*events)
    sent = int(user32.SendInput(len(events), array, ctypes.sizeof(_Input)))
    if sent != len(events):
        raise TypingError(f"Windows accepted {sent} of {len(events)} keystrokes")
    log.info("desktop.typed", characters=len(text), window=expected_window, enter=press_enter)
    return len(text)
