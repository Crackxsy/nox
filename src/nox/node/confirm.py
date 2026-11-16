"""The confirmation on the device itself, for high risk (owner decision D1, spec C §1.6 step 5).

"Nox möchte auf diesem Gerät …: Erlauben? Ablehnen." with a 60 s countdown and **deny** as the
default - no answer, a closed dialog or any error is a refusal. The core never shows this prompt
elsewhere; the outcome goes back as `nox/confirm_result` and is audited on both sides. An "allow"
faster than `nox.node.agent.MIN_DECISION_S` counts as a reflex and is a "no" on every platform.

* **Windows**: a system popup with "No" as the default button.
* **macOS**: `osascript` `display dialog` with the buttons "Ablehnen" / "Erlauben", "Ablehnen" as
  the default *and* the cancel button, giving up after 60 s.
* **Linux desktop**: `zenity --question --default-cancel` (verifiably "No" by default); on KDE
  without zenity `kdialog --warningyesno`, whose default button is "No" (KMessageBox `Dangerous`).
  The installer names zenity as a prerequisite.

A device without a screen cannot ask: high risk is refused there unless `node.yaml` has
`trust_core_for_high: true`, confirmed locally (a documented residual risk). A device *with* a
screen never trusts the core instead: without a working dialog program it refuses (`screen`).

Whether there is a screen is read when asked, never frozen at start (`has_screen`): a desktop
node that started before the graphical session takes the session's display from its user
manager once the session is there, and loses it when the session ends.

The text is passed to the dialog as data (environment / argv), never as script text.
"""

from __future__ import annotations

import asyncio
import html
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal, Protocol

__all__ = [
    "CONFIRM_TIMEOUT_S",
    "Answer",
    "Confirmer",
    "PlatformConfirmer",
    "has_screen",
    "wayland_session",
]

CONFIRM_TIMEOUT_S = 60
Answer = Literal["allow", "deny", "timeout"]
_CREATE_NO_WINDOW = 0x08000000
#: The session variables a desktop node takes from its user manager when it started without them.
SESSION_VARIABLES = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "DBUS_SESSION_BUS_ADDRESS",
)
#: How long a look at the user manager's environment is reused.
_SESSION_TTL_S = 5.0
_session_cache: list[tuple[float, dict[str, str]]] = []

#: WScript.Shell.Popup: 60 s timeout, Yes/No buttons (4), a question icon (32), default "No"
#: (256). Returns 6 for yes, 7 for no, -1 on timeout. Text and title come from the environment.
_WINDOWS_POPUP = (
    "$s=New-Object -ComObject WScript.Shell;"
    "$r=$s.Popup($env:NOX_CONFIRM_TEXT,[int]$env:NOX_CONFIRM_TIMEOUT,"
    "'Nox',4+32+256+4096);"
    "exit $(if($r -eq 6){0}elseif($r -eq -1){2}else{1})"
)
#: macOS: the text and the timeout are arguments, never script text. "Ablehnen" is the default
#: button (Return) and the cancel button (Escape, an error -128 - a "no"); only the exact word
#: "allow" on stdout is a yes.
MAC_DIALOG = (
    "on run argv\n"
    'set answer to display dialog (item 1 of argv) with title "Nox" '
    'buttons {"Ablehnen", "Erlauben"} default button "Ablehnen" cancel button "Ablehnen" '
    "giving up after ((item 2 of argv) as integer) with icon caution\n"
    'if gave up of answer then return "timeout"\n'
    'if button returned of answer is "Erlauben" then return "allow"\n'
    'return "deny"\n'
    "end run"
)
OSASCRIPT = "/usr/bin/osascript"


class Confirmer(Protocol):
    @property
    def available(self) -> bool: ...
    async def ask(self, text: str, *, timeout_s: int = CONFIRM_TIMEOUT_S) -> Answer: ...


def _socket_there(variable: str, value: str) -> bool:
    """Whether the display a variable names is still there (a local X socket, a Wayland socket)."""
    if variable == "WAYLAND_DISPLAY":
        runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
        return (
            bool(runtime or value.startswith("/"))
            and Path(value if value.startswith("/") else os.path.join(runtime, value)).exists()
        )
    host, _, number = value.partition(":")
    if host not in ("", "unix") or not number:
        return True  # a remote or TCP display: nothing local to look at
    socket = Path("/tmp/.X11-unix") / f"X{number.split('.', 1)[0]}"  # noqa: S108 - X's own folder
    return socket.exists()


def _user_manager_environment() -> dict[str, str]:
    """`systemctl --user show-environment`, the session variables only (cached briefly). Empty
    outside a user session (a headless node's system unit has none)."""
    now = time.monotonic()
    if _session_cache and now - _session_cache[0][0] < _SESSION_TTL_S:
        return _session_cache[0][1]
    found: dict[str, str] = {}
    systemctl = shutil.which("systemctl")
    if systemctl and os.environ.get("XDG_RUNTIME_DIR"):
        try:
            out = subprocess.run(  # noqa: S603 - a fixed program and arguments
                [systemctl, "--user", "show-environment"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            name, _, value = line.partition("=")
            if name in SESSION_VARIABLES and value:
                found[name] = value
    _session_cache[:] = [(now, found)]
    return found


def _display_in(env: dict[str, str] | os._Environ[str]) -> bool:
    return any(
        bool(env.get(variable)) and _socket_there(variable, str(env.get(variable)))
        for variable in ("WAYLAND_DISPLAY", "DISPLAY")
    )


def has_screen() -> bool:
    """Whether a person can see a dialog on this device right now. Windows and macOS: yes. Linux:
    a display (`DISPLAY` or `WAYLAND_DISPLAY`) whose socket is there - from this process's
    environment or, for a node started before the session, from its user manager (then taken
    over into this process, so dialogs, notices and captures reach the session)."""
    if sys.platform in ("win32", "darwin"):
        return True
    if _display_in(os.environ):
        return True
    session = _user_manager_environment()
    if _display_in(session):
        os.environ.update(session)
        return True
    return False


def wayland_session() -> bool:
    """Whether this Linux desktop session is Wayland (X11 programs then see only XWayland)."""
    return bool(os.environ.get("WAYLAND_DISPLAY")) or (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )


def _powershell() -> Path:
    system_root = Path(os.environ.get("SystemRoot", "C:\\Windows"))
    return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def kdialog_text(text: str) -> str:
    """kdialog shows rich text when it looks like it: forced, with everything escaped, so the
    text is shown exactly as written and can never format itself."""
    return "<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>"


class PlatformConfirmer:
    """The dialog on this device (see the module docstring); unavailable where none can be shown."""

    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform or sys.platform

    @property
    def screen(self) -> bool:
        """Whether this device has a screen now (then the core is never trusted instead)."""
        return has_screen()

    def _linux_program(self) -> str | None:
        if not has_screen():
            return None
        return shutil.which("zenity") or shutil.which("kdialog")

    @property
    def available(self) -> bool:
        if self.platform == "win32":
            return True
        if self.platform == "darwin":
            return Path(OSASCRIPT).exists()
        if self.platform.startswith("linux"):
            return self._linux_program() is not None
        return False

    def argv(self, text: str, timeout_s: int) -> list[str] | None:
        """The dialog's command line - the text is one argument, never script text."""
        if self.platform == "win32":
            return [str(_powershell()), "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_POPUP]
        if self.platform == "darwin":
            return [OSASCRIPT, "-e", MAC_DIALOG, text, str(timeout_s)]
        program = self._linux_program()
        if program is None:
            return None
        if os.path.basename(program) == "zenity":
            return [
                program,
                "--question",
                "--title=Nox",
                f"--timeout={timeout_s}",
                "--ok-label=Erlauben",
                "--cancel-label=Ablehnen",
                "--default-cancel",
                "--no-markup",
                "--text",
                text,
            ]
        return [
            program,
            "--title",
            "Nox",
            "--yes-label",
            "Erlauben",
            "--no-label",
            "Ablehnen",
            "--warningyesno",
            kdialog_text(text),
        ]

    async def ask(self, text: str, *, timeout_s: int = CONFIRM_TIMEOUT_S) -> Answer:
        if not self.available:
            return "deny"
        argv = self.argv(text, timeout_s)
        if argv is None:
            return "deny"
        env = {**os.environ, "NOX_CONFIRM_TEXT": text, "NOX_CONFIRM_TIMEOUT": str(timeout_s)}
        mac = self.platform == "darwin"
        kwargs: dict[str, object] = {
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE if mac else subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if self.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            process = await asyncio.create_subprocess_exec(*argv, **kwargs)  # type: ignore[arg-type]
        except OSError:
            return "deny"
        # kdialog has no countdown of its own: it is closed at the deadline (a "timeout").
        kdialog = os.path.basename(argv[0]) == "kdialog"
        out: bytes | None = None
        try:
            limit = timeout_s if kdialog else timeout_s + 5
            if mac:
                out, _ = await asyncio.wait_for(process.communicate(), limit)
            else:
                await asyncio.wait_for(process.wait(), limit)
        except TimeoutError:
            process.kill()
            return "timeout"
        except asyncio.CancelledError:
            # Stopped while the question was open: the dialog goes away with the call.
            process.kill()
            raise
        return answer_of(argv[0], process.returncode, out)


def answer_of(program: str, code: int | None, out: bytes | None) -> Answer:
    """What a dialog program's exit (and, for `osascript`, its one word on stdout) means - a
    "no" for everything that is not an unmistakable yes or timeout."""
    name = os.path.basename(program).lower()
    if name == "osascript":
        word = (out or b"").decode("utf-8", "replace").strip()
        if code == 0 and word in ("allow", "timeout"):
            return "allow" if word == "allow" else "timeout"
        return "deny"  # "Ablehnen", Escape (-128), "no user interaction allowed", any error
    if code == 0:
        return "allow"
    if name == "kdialog":
        return "deny"  # 1 = "Ablehnen", anything else an error
    # zenity: 5 = timeout; the Windows script: 2 = timeout; anything else is a "no".
    return "timeout" if code in (2, 5) else "deny"
