"""The voice worker's own account of its health, as the core last heard it.

The worker knows things the core cannot see from outside: whether the microphone still delivers
audio, whether a Whisper model is on disk, whether the wake word is matched acoustically or on
transcripts, and that there is no echo cancellation. It sends that with every heartbeat; this
report keeps the latest copy and folds it into one `(status, reason)` for the `voice` health check.

A report that stopped arriving is not trusted: after `STALE_AFTER_S` without a heartbeat the check
says so instead of repeating the last good answer.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from nox.core.events import HealthStatus

__all__ = ["STALE_AFTER_S", "VoiceHealthReport"]

#: Three missed heartbeats at the worker's two-second interval, plus slack for a busy loop.
STALE_AFTER_S = 10.0

_SEVERITY = {HealthStatus.AVAILABLE: 0, HealthStatus.LIMITED: 1, HealthStatus.UNAVAILABLE: 2}


class VoiceHealthReport:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._components: dict[str, tuple[HealthStatus, str]] = {}
        self._received_at: float | None = None

    @property
    def components(self) -> dict[str, tuple[HealthStatus, str]]:
        return dict(self._components)

    def update(self, components: Mapping[str, tuple[HealthStatus, str]]) -> bool:
        """Store a heartbeat's report. True when a component's status changed."""
        before = {name: status for name, (status, _) in self._components.items()}
        self._components = dict(components)
        self._received_at = self._clock()
        after = {name: status for name, (status, _) in self._components.items()}
        return before != after

    def clear(self) -> None:
        """The worker is gone; nothing it said still holds."""
        self._components = {}
        self._received_at = None

    def summary(self) -> tuple[HealthStatus, str] | None:
        """The worst component and every reason that is not `available`; None before any report."""
        if self._received_at is None:
            return None
        silent_s = self._clock() - self._received_at
        if silent_s > STALE_AFTER_S:
            return HealthStatus.LIMITED, f"no heartbeat from the voice worker for {silent_s:.0f} s"
        if not self._components:
            return None
        worst = max((status for status, _ in self._components.values()), key=_SEVERITY.__getitem__)
        reasons = [
            f"{name}: {reason}"
            for name, (status, reason) in sorted(self._components.items())
            if status is not HealthStatus.AVAILABLE
        ]
        if not reasons:
            return HealthStatus.AVAILABLE, "worker registered"
        return worst, "; ".join(reasons)
