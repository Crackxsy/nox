"""Which processes are running and which of them are Nox's own - read from the operating system.

Shared by the desktop tools on a core (`nox.desktop.install`) and by a node's game lock and
computer-use session (`nox.node.device.world`): both ask the same questions about the same
machine. Owns the reading only; what a process means (a game, anti-cheat software) is decided by
the callers.
"""

from __future__ import annotations

import os

import psutil


def running_names_on_machine() -> set[str]:
    names: set[str] = set()
    for process in psutil.process_iter(["name"]):
        try:
            names.add(str(process.info.get("name") or "").lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return names


def process_name_of(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.Error, ValueError):
        return ""


def nox_pids() -> set[int]:
    """This core and every process it started (workers, the shell when it is a child)."""
    me = psutil.Process(os.getpid())
    pids = {me.pid}
    try:
        pids |= {child.pid for child in me.children(recursive=True)}
        parent = me.parent()
        if parent is not None and "nox" in " ".join(parent.cmdline()).lower():
            pids.add(parent.pid)
    except psutil.Error:
        pass
    return pids


def is_nox_process(pid: int) -> bool:
    """Nox's own processes - the core and its children, or a Nox process started separately
    (the shell from a shortcut): judged by the process, never by a window title alone."""
    if pid in nox_pids():
        return True
    try:
        process = psutil.Process(pid)
        name = process.name().lower()
        if not name.startswith(("python", "pythonw", "nox")):
            return False
        return any("nox" in part.lower() for part in process.cmdline()[1:3])
    except (psutil.Error, ValueError, OSError):
        return False
