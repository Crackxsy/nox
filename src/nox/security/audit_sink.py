"""Two wrappers around an `AuditLog`, so the rest of the code never has to think about either.

`QueuedAuditLog` moves the SQLite insert off the event loop. A permission check and every outbound
request audit their decision, and both happen on request paths; committing a row inline meant a
disk write in the middle of the loop. Entries are handed to one writer thread, which preserves
their order - the hash chain depends on it. **Nothing is ever dropped**: when the queue is full the
caller writes the entry itself and pays the latency, which is the right trade when the alternative
is an audit log with holes in it.

`SafeAuditLog` replaces the four hand-written "append if a log was injected, and never let an audit
write break the caller" wrappers that had grown in the permission engine, the privacy service, the
kill switch and the PIN manager. It keeps the real keyword signature, so the two `type: ignore`
comments those wrappers needed are gone.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

from nox.security._logging import get_logger
from nox.security.model import AuditLog

log = get_logger(__name__)

__all__ = ["QueuedAuditLog", "SafeAuditLog"]

#: Entries the queue holds before a writer starts paying for its own entries inline. About one
#: second of a very busy permission path; past that, back-pressure is more honest than a backlog.
DEFAULT_QUEUE_SIZE = 2048

#: Seconds `stop()` waits for the backlog to drain before it gives up and says so.
DEFAULT_DRAIN_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class _Entry:
    """One queued `AuditLog.append` call."""

    actor: str
    tool: str
    action: str
    target: str
    decision: str
    result: str
    task_id: str | None
    details: dict[str, str] | None


class QueuedAuditLog:
    """An `AuditLog` that appends from a single background thread.

    `append` returns 0: the sequence number is assigned by the writer thread, so there is none to
    return yet. No caller on a request path uses it; the ones that do - boot and shutdown - write
    through the underlying log directly.
    """

    def __init__(
        self,
        inner: AuditLog,
        *,
        max_queue: int = DEFAULT_QUEUE_SIZE,
        drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    ) -> None:
        self._inner = inner
        self._drain_timeout_s = drain_timeout_s
        self._queue: queue.Queue[_Entry | None] = queue.Queue(maxsize=max_queue)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="nox-audit-writer", daemon=True)
        self._thread.start()

    @property
    def inner(self) -> AuditLog:
        """The underlying log, for a caller that needs the sequence number or a blocking write."""
        return self._inner

    def append(
        self,
        *,
        actor: str,
        tool: str,
        action: str,
        target: str,
        decision: str,
        result: str,
        task_id: str | None = None,
        details: dict[str, str] | None = None,
    ) -> int:
        entry = _Entry(
            actor=actor,
            tool=tool,
            action=action,
            target=target,
            decision=decision,
            result=result,
            task_id=task_id,
            details=details,
        )
        if self._stopped.is_set():
            return self._write(entry)
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            log.warning("audit.queue_full", action=action, note="writing inline")
            return self._write(entry)
        return 0

    def verify_chain(self) -> bool:
        return self._inner.verify_chain()

    def flush(self, timeout_s: float | None = None) -> bool:
        """Block until the backlog is written. False when it was still draining."""
        deadline = timeout_s if timeout_s is not None else self._drain_timeout_s
        step = 0.01
        waited = 0.0
        while not self._queue.empty() and waited < deadline:
            time.sleep(step)
            waited += step
        return self._queue.empty()

    def stop(self) -> bool:
        """Drain what is queued, stop the writer, and report whether everything was written."""
        if self._stopped.is_set():
            return True
        self._stopped.set()
        self._queue.put(None)
        self._thread.join(timeout=self._drain_timeout_s)
        drained = not self._thread.is_alive()
        if not drained:
            log.error("audit.drain_timeout", timeout_s=self._drain_timeout_s)
        return drained

    def _run(self) -> None:
        while True:
            entry = self._queue.get()
            if entry is None:
                return
            self._write(entry)

    def _write(self, entry: _Entry) -> int:
        try:
            return self._inner.append(
                actor=entry.actor,
                tool=entry.tool,
                action=entry.action,
                target=entry.target,
                decision=entry.decision,
                result=entry.result,
                task_id=entry.task_id,
                details=entry.details,
            )
        except Exception as exc:  # noqa: BLE001 - a failed write must not stop the writer thread
            log.error(
                "audit.write_failed", action=entry.action, error=f"{type(exc).__name__}: {exc}"
            )
            return 0


class SafeAuditLog:
    """Appends to `inner` when there is one; logs and swallows a write failure.

    Security services take an optional audit log - a unit test rarely wants one - and none of them
    may fail because a log write did. Both facts belong here, once, rather than in a private
    helper per service.
    """

    def __init__(self, inner: AuditLog | None, *, tool: str = "security") -> None:
        self._inner = inner
        self._tool = tool

    @property
    def enabled(self) -> bool:
        return self._inner is not None

    def append(
        self,
        *,
        actor: str,
        action: str,
        target: str = "",
        tool: str | None = None,
        decision: str = "allow",
        result: str = "ok",
        task_id: str | None = None,
        details: dict[str, str] | None = None,
    ) -> None:
        if self._inner is None:
            return
        try:
            self._inner.append(
                actor=actor,
                tool=tool or self._tool,
                action=action,
                target=target,
                decision=decision,
                result=result,
                task_id=task_id,
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 - the caller's own work must still complete
            log.error("audit.append_failed", action=action, error=f"{type(exc).__name__}: {exc}")
