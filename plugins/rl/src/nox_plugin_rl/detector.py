"""Game detection (ST-12-01): read-only process-list polling for Rocket League. No window-handle
API, no memory read, no injection - `psutil.process_iter` only (Security Model §10)."""

from __future__ import annotations

from collections.abc import Iterable

import psutil


class GameDetector:
    """Polls the process list for any of `process_names` (case-insensitive). Pure/testable: the
    process list is injected via `process_names_fn` so tests never depend on a real game running."""

    def __init__(
        self,
        process_names: Iterable[str],
        *,
        list_process_names: object | None = None,
    ) -> None:
        self._names = {n.lower() for n in process_names}
        self._list_process_names = list_process_names or _psutil_process_names
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def poll(self) -> bool:
        """Returns True if a watched process is running right now (updates `.running`)."""
        names = self._list_process_names()  # type: ignore[operator]
        found = any(str(n).lower() in self._names for n in names)
        self._running = found
        return found


def _psutil_process_names() -> list[str]:
    names: list[str] = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name") or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name:
            names.append(name)
    return names
