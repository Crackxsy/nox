"""What keeps Nox's hands off a machine where a game guards against input, whatever the user put
on the watched-game list: anti-cheat processes, services and kernel drivers, and a full-screen
game or presentation in front.

Only observation: names of running processes, the state of a few named services and drivers
(`QueryServiceStatus`), and the shell's own notification state (`SHQueryUserNotificationState`).
Nothing here reads another process's memory or sends input (Security Model §10). Every check
that cannot answer says "locked" - a lock that fails open is no lock.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterable
from typing import Any

__all__ = [
    "ANTI_CHEAT_PREFIXES",
    "ANTI_CHEAT_PROCESSES",
    "ANTI_CHEAT_SERVICES",
    "LOCKING_NOTIFICATION_STATES",
    "fullscreen_state",
    "is_anti_cheat",
    "running_services",
]

#: Anti-cheat processes (lower case). While one runs, a game that guards against input runs.
ANTI_CHEAT_PROCESSES = frozenset(
    {
        "easyanticheat.exe",
        "easyanticheat_eos.exe",
        "beservice.exe",
        "beservice_x64.exe",
        "vgc.exe",
        "faceitclient.exe",
        "faceitservice.exe",
        "eaanticheat.gameservice.exe",
        "sguard64.exe",
        "sguardsvc64.exe",
        "pnkbstra.exe",
        "pnkbstrb.exe",
        "xxd-0.xem",
        "xigncode.exe",
        "gamemon.des",
        "gamemon64.des",
    }
)
#: Families with numbered or versioned process names (`GameMon64.exe`, `PnkBstrB.exe`).
ANTI_CHEAT_PREFIXES: tuple[str, ...] = ("gamemon", "pnkbstr", "xigncode", "xxd-", "sguard")
#: Anti-cheat services and kernel drivers by their service name (the SCM compares without case).
ANTI_CHEAT_SERVICES: tuple[str, ...] = (
    "vgc",
    "vgk",
    "EasyAntiCheat",
    "EasyAntiCheat_EOS",
    "EasyAntiCheatSys",
    "BEService",
    "BEDaisy",
    "FACEIT",
    "FACEITService",
    "faceit",
    "ACE-BASE",
    "ACE-GAME",
    "AntiCheatExpert Service",
    "SGuardSvc",
    "npggsvc",
    "GameMon",
    "PnkBstrA",
    "PnkBstrB",
    "xhunter1",
    "XIGNCODE",
    "mhyprot",
    "mhyprot2",
    "mhyprot3",
    "atvi-randgrid_sr",
    "atvi-randgrid_msstore",
)
#: `SHQueryUserNotificationState`: QUNS_BUSY (2), QUNS_RUNNING_D3D_FULL_SCREEN (3),
#: QUNS_PRESENTATION_MODE (4) - something owns the screen, very likely a game.
LOCKING_NOTIFICATION_STATES = frozenset({2, 3, 4})
SERVICE_RUNNING = 4
SERVICE_QUERY_STATUS = 0x0004
SC_MANAGER_CONNECT = 0x0001
ERROR_SERVICE_DOES_NOT_EXIST = 1060


def is_anti_cheat(process_name: str) -> bool:
    name = process_name.strip().lower()
    return name in ANTI_CHEAT_PROCESSES or name.startswith(ANTI_CHEAT_PREFIXES)


def fullscreen_state() -> int:
    """The shell's notification state (Windows). Raises when it cannot be read."""
    if sys.platform != "win32" or not hasattr(ctypes, "windll"):
        raise OSError("no notification state off Windows")
    state = ctypes.c_int(0)
    windll: Any = ctypes.windll  # type: ignore[attr-defined,unused-ignore]
    result = windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
    if result != 0:
        raise OSError(f"SHQueryUserNotificationState failed: {result:#x}")
    return int(state.value)


class _ServiceStatus(ctypes.Structure):
    _fields_ = (
        ("dwServiceType", ctypes.c_ulong),
        ("dwCurrentState", ctypes.c_ulong),
        ("dwControlsAccepted", ctypes.c_ulong),
        ("dwWin32ExitCode", ctypes.c_ulong),
        ("dwServiceSpecificExitCode", ctypes.c_ulong),
        ("dwCheckPoint", ctypes.c_ulong),
        ("dwWaitHint", ctypes.c_ulong),
    )


def running_services(names: Iterable[str] = ANTI_CHEAT_SERVICES) -> set[str]:
    """Which of these services or drivers run now (Windows). A service that does not exist is
    not running; anything else that cannot be read raises (the caller locks)."""
    if sys.platform != "win32" or not hasattr(ctypes, "windll"):
        return set()
    advapi: Any = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    advapi.OpenSCManagerW.restype = ctypes.c_void_p
    advapi.OpenServiceW.restype = ctypes.c_void_p
    advapi.OpenServiceW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong)
    advapi.QueryServiceStatus.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    advapi.CloseServiceHandle.argtypes = (ctypes.c_void_p,)
    manager = advapi.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not manager:
        raise OSError(f"OpenSCManager failed: {ctypes.get_last_error()}")  # type: ignore[attr-defined,unused-ignore]
    found: set[str] = set()
    try:
        for name in names:
            service = advapi.OpenServiceW(manager, name, SERVICE_QUERY_STATUS)
            if not service:
                error = ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
                if error == ERROR_SERVICE_DOES_NOT_EXIST:
                    continue
                raise OSError(f"OpenService({name}) failed: {error}")
            try:
                status = _ServiceStatus()
                if not advapi.QueryServiceStatus(service, ctypes.byref(status)):
                    raise OSError(f"QueryServiceStatus({name}) failed")
                if status.dwCurrentState == SERVICE_RUNNING:
                    found.add(name)
            finally:
                advapi.CloseServiceHandle(service)
    finally:
        advapi.CloseServiceHandle(manager)
    return found
