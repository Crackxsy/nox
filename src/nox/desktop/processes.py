"""Processes: what is running, and the one thing Nox may do about it.

Listing is the easy half, and even that needs a decision: a Windows machine has three hundred
processes and a hundred of them are `svchost.exe`. An unfiltered list is useless to a model and
expensive to send, so the list is sorted by memory and capped, and it says how many there were.

Stopping is `TerminateProcess` by way of psutil, and it is not polite: the program gets no chance to
save anything. That is why `desktop.window_close` exists next to it - asking a program to close is
almost always what a person means, and killing it is what you do when asking did not work. The tool
descriptions say so, in those words, because a model choosing the wrong one of those two loses work.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import psutil

from nox.core.logging import get_logger
from nox.desktop.boundary import BoundaryError, check_process

log = get_logger(__name__)

__all__ = ["list_processes", "own_pids", "stop_process"]

#: How long a terminated process is given to disappear before the answer says it is still there.
TERMINATE_TIMEOUT_S = 5.0


def own_pids() -> set[int]:
    """Nox's own process and its children, so the boundary can refuse them by pid.

    Its children are the ones that matter: the voice worker and the plugin workers are where a
    "close the thing that is using my microphone" request would otherwise land.
    """
    pids = {os.getpid()}
    try:
        me = psutil.Process()
        pids.add(me.pid)
        pids.update(child.pid for child in me.children(recursive=True))
        parent = me.parent()
        if parent is not None:
            pids.add(parent.pid)
    except psutil.Error:  # pragma: no cover - a process table that will not answer
        pass
    return pids


def _snapshot(limit: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            rows.append(
                {
                    "pid": int(info["pid"]),
                    "name": str(info.get("name") or ""),
                    "memory_mb": round((memory.rss if memory else 0) / (1024 * 1024), 1),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda row: row["memory_mb"], reverse=True)
    return {
        "ok": True,
        "processes": rows[:limit],
        "total": len(rows),
        "truncated": len(rows) > limit,
    }


async def list_processes(*, limit: int) -> dict[str, Any]:
    """The processes using the most memory, capped. Reading the table is blocking work."""
    return await asyncio.to_thread(_snapshot, limit)


def _process(pid: int) -> psutil.Process:
    """One named seam for looking up another process.

    A test replaces this rather than `psutil.Process` itself: patching that breaks psutil's own
    `isinstance` checks, and `own_pids()` needs the real lookup to keep working anyway - it is what
    the boundary uses to refuse ending Nox.
    """
    return psutil.Process(pid)


def _terminate(pid: int, game_pids: set[int]) -> dict[str, Any]:
    try:
        process = _process(pid)
        name = process.name()
    except psutil.NoSuchProcess:
        return {"ok": False, "error": f"there is no process with the id {pid}"}
    except psutil.AccessDenied:
        return {"ok": False, "error": f"process {pid} cannot be read with these rights"}

    try:
        check_process(pid, name, own_pids=own_pids(), game_pids=game_pids)
    except BoundaryError as exc:
        log.info("desktop.stop_refused", pid=pid, name=name, reason=str(exc))
        return {"ok": False, "error": str(exc)}

    try:
        process.terminate()
        process.wait(timeout=TERMINATE_TIMEOUT_S)
    except psutil.AccessDenied:
        return {
            "ok": False,
            "error": f"{name} refused to be ended with these rights (no elevation is requested)",
        }
    except psutil.TimeoutExpired:
        # Deliberately not escalating to kill(): a program that ignores a terminate is either busy
        # saving or stuck, and deciding which is not this code's call.
        return {"ok": False, "error": f"{name} did not end within {TERMINATE_TIMEOUT_S:.0f}s"}
    log.info("desktop.stopped", pid=pid, name=name)
    return {"ok": True, "pid": pid, "name": name}


async def stop_process(pid: int, *, game_pids: set[int]) -> dict[str, Any]:
    """End one process. Unsaved work in it is lost; that is what the tool description says."""
    return await asyncio.to_thread(_terminate, pid, game_pids)
