"""The confirmation on the device itself, for high risk (owner decision D1, spec C §1.6 step 5).

"Nox möchte auf diesem Gerät …: Erlauben? Ablehnen." with a 60 s countdown and **deny** as the
default - no answer, a closed dialog or any error is a refusal. The core never shows this prompt
elsewhere; the outcome goes back as `nox/confirm_result` and is audited on both sides.

A device without a screen cannot ask: high risk is refused there unless `node.yaml` has
`trust_core_for_high: true`, confirmed locally (a documented residual risk).

The text is passed to the dialog as data (environment / argv), never as script text.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal, Protocol

__all__ = ["CONFIRM_TIMEOUT_S", "Answer", "Confirmer", "PlatformConfirmer", "has_screen"]

CONFIRM_TIMEOUT_S = 60
Answer = Literal["allow", "deny", "timeout"]
_CREATE_NO_WINDOW = 0x08000000

#: WScript.Shell.Popup: 60 s timeout, Yes/No buttons (4), a question icon (32), default "No"
#: (256). Returns 6 for yes, 7 for no, -1 on timeout. Text and title come from the environment.
_WINDOWS_POPUP = (
    "$s=New-Object -ComObject WScript.Shell;"
    "$r=$s.Popup($env:NOX_CONFIRM_TEXT,[int]$env:NOX_CONFIRM_TIMEOUT,"
    "'Nox',4+32+256+4096);"
    "exit $(if($r -eq 6){0}elseif($r -eq -1){2}else{1})"
)


class Confirmer(Protocol):
    @property
    def available(self) -> bool: ...
    async def ask(self, text: str, *, timeout_s: int = CONFIRM_TIMEOUT_S) -> Answer: ...


def has_screen() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _powershell() -> Path:
    system_root = Path(os.environ.get("SystemRoot", "C:\\Windows"))
    return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


class PlatformConfirmer:
    """Windows: a system popup. Linux desktop: `zenity` or `kdialog`. Elsewhere: unavailable."""

    @property
    def available(self) -> bool:
        if sys.platform == "win32":
            return True
        if sys.platform == "linux" and has_screen():
            return bool(shutil.which("zenity") or shutil.which("kdialog"))
        return False

    async def ask(self, text: str, *, timeout_s: int = CONFIRM_TIMEOUT_S) -> Answer:
        if not self.available:
            return "deny"
        env = {
            **os.environ,
            "NOX_CONFIRM_TEXT": text,
            "NOX_CONFIRM_TIMEOUT": str(timeout_s),
        }
        kwargs: dict[str, object] = {"env": env, "stdout": subprocess.DEVNULL}
        if sys.platform == "win32":
            argv = [str(_powershell()), "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_POPUP]
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        elif shutil.which("zenity"):
            argv = [
                "zenity",
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
        else:
            # kdialog reads rich text: the text is shown literally, never as markup.
            plain = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            argv = ["kdialog", "--title", "Nox", "--yesno", plain]
        try:
            process = await asyncio.create_subprocess_exec(*argv, **kwargs)  # type: ignore[arg-type]
        except OSError:
            return "deny"
        try:
            code = await asyncio.wait_for(process.wait(), timeout_s + 5)
        except TimeoutError:
            process.kill()
            return "timeout"
        except asyncio.CancelledError:
            # Stopped while the question was open: the dialog goes away with the call.
            process.kill()
            raise
        if code == 0:
            return "allow"
        # zenity: 5 = timeout; the Windows script: 2 = timeout; anything else is a "no".
        return "timeout" if code in (2, 5) else "deny"
