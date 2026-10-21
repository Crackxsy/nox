"""The node's own name for Windows notifications (AppUserModelID).

A toast on Windows is shown under the app that sends it. Without an ID of its own, a node's
notice would appear as "Windows PowerShell" - easy to mistake, easy to imitate. The node-mode
installer registers `NODE_APP_ID` for the user (`HKCU\\Software\\Classes\\AppUserModelId\\Nox.Node`,
display name "Nox", 64-bit registry view) and gives the Start-menu entry the same ID; notices then
carry Nox's name. Until that key exists (a node started from a checkout), the PowerShell ID is used.
"""

from __future__ import annotations

import sys

__all__ = ["NODE_APP_ID", "POWERSHELL_APP_ID", "REGISTRY_KEY", "toast_app_id"]

NODE_APP_ID = "Nox.Node"
POWERSHELL_APP_ID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
)
REGISTRY_KEY = f"Software\\Classes\\AppUserModelId\\{NODE_APP_ID}"


def toast_app_id() -> str:
    if sys.platform != "win32":
        return NODE_APP_ID
    import winreg  # noqa: PLC0415 - Windows only

    try:
        # The 64-bit view explicitly: a 32-bit process would otherwise look somewhere else.
        winreg.CloseKey(
            winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
            )
        )
    except OSError:
        return POWERSHELL_APP_ID
    return NODE_APP_ID
