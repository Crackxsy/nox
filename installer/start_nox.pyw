"""Start Nox from an installed copy: `pythonw.exe start_nox.pyw` (Start menu, desktop, autostart).

An installed Nox runs on an embedded Python whose `python313._pth` decides `sys.path` completely:
neither the working directory nor a script's folder is added on their own. Older installers did
not put the folder holding the `nox` package there, and `pythonw.exe -m nox.supervisor` then
failed on `import nox` - silently, because `pythonw` has no console to print the error to.

This launcher makes neither mistake possible again: it puts its own folder (the one holding
`nox/`, `config/` and `plugins/`) on `sys.path` itself, and if Nox cannot start it writes the
reason to `%LOCALAPPDATA%\\Nox\\startup-error.log` and says so in a message box, instead of doing
nothing at all.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
#: Set to "1" to skip the message box (used by tests, never by the shortcuts).
NO_DIALOG_ENV = "NOX_LAUNCH_NO_DIALOG"


def error_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Nox" / "startup-error.log"


def report_failure(exc: BaseException) -> Path:
    """Write the traceback where the user can find it, and tell them once."""
    path = error_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(traceback.format_exception(exc))
    path.write_text(
        f"Nox could not start.\npython: {sys.executable}\napp: {APP_DIR}\n\n{text}",
        encoding="utf-8",
    )
    if os.environ.get(NO_DIALOG_ENV) != "1" and sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            None,
            f"Nox konnte nicht starten / Nox could not start.\n\n{exc}\n\nDetails: {path}",
            "Nox",
            0x10,  # MB_ICONERROR
        )
    return path


def main() -> int:
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    try:
        from nox.supervisor.main import main as supervisor_main

        return int(supervisor_main() or 0)
    except KeyboardInterrupt:
        return 130
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 1
    except BaseException as exc:  # noqa: BLE001 - every failure must be reported, not lost
        report_failure(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
