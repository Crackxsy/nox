"""The security state that has to survive a restart: privacy mode, panic, and the kill switch.

Everything else in the security core is rebuilt from configuration on every boot. These three are
not configuration - they are what the user (or a security event) decided while Nox was running -
and rebuilding them from configuration made Nox *less* private after a crash or a watchdog
restart: an OFFLINE set from the tray came back as the configured BALANCED, and a kill switch came
back released, without the PIN a security-path resume needs.

This module owns the shape of that state, the store protocol it is kept in, the boot-time restore
and the recorder that writes every change. It does not own the table (`nox.data.security_repos`)
or the services whose state it mirrors (`PrivacyService`, `KillSwitchService`).

Rules:

- **Restore before anything can act.** `restore_security_state` runs inside
  `SecurityContext.build`, before the context is returned to anything that could spawn, connect
  or answer.
- **Nothing stored** (a fresh install, or the first boot after an upgrade) is not an error: the
  configured values apply.
- **Unreadable is never permissive.** If the stored row cannot be read or does not validate, the
  privacy mode is the strictest of the configured one and whatever part of the stored one could
  still be read, and the kill switch is engaged as a security-path kill: "cannot tell whether the
  kill switch was engaged" is not "it was not".
- **Every change is written when it happens**, in order, off the event loop. A write that fails is
  logged and leaves the in-memory state as it is; the stored state then lags, which errs towards
  whatever was stricter before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from nox.core.state import PrivacyMode
from nox.security._logging import get_logger
from nox.security.gate import strictest_privacy_mode

if TYPE_CHECKING:
    from nox.security.killswitch import KillSwitchService
    from nox.security.privacy import PrivacyService

log = get_logger(__name__)

__all__ = [
    "UNREADABLE_KILL_ORIGIN",
    "InMemorySecurityStateStore",
    "PersistedSecurityState",
    "RestoreOutcome",
    "SecurityStateRecorder",
    "SecurityStateStore",
    "SecurityStateUnreadableError",
    "restore_security_state",
]

#: The kill-switch origin recorded when the stored state could not be read at boot.
UNREADABLE_KILL_ORIGIN = "state"


class PersistedSecurityState(BaseModel):
    """One snapshot of the state that survives a restart."""

    model_config = ConfigDict(frozen=True)

    privacy_mode: PrivacyMode
    panic: bool = False
    kill_engaged: bool = False
    #: Whether the engaged kill came from the security path, so resuming needs the PIN.
    kill_security_path: bool = False
    kill_origin: str = ""
    kill_reason: str = ""


class SecurityStateUnreadableError(RuntimeError):
    """The stored state exists but cannot be read or does not validate.

    `privacy_mode` carries the stored mode when that one column could still be parsed, so the
    restore can take the stricter of it and the configured mode.
    """

    def __init__(self, reason: str, *, privacy_mode: PrivacyMode | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.privacy_mode = privacy_mode


class SecurityStateStore(Protocol):
    """Where the snapshot lives. `load` returns None when nothing was ever stored."""

    def load(self) -> PersistedSecurityState | None: ...
    def save(self, state: PersistedSecurityState) -> None: ...


class InMemorySecurityStateStore:
    """Keeps the snapshot for the lifetime of the object: for tests and database-less tools."""

    def __init__(self, state: PersistedSecurityState | None = None) -> None:
        self.state = state
        self.saves = 0

    def load(self) -> PersistedSecurityState | None:
        return self.state

    def save(self, state: PersistedSecurityState) -> None:
        self.state = state
        self.saves += 1


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """What the boot restored, for the log line and the caller's safe-mode decision."""

    source: str  # "stored" | "configured" | "unreadable"
    privacy_mode: PrivacyMode
    kill_engaged: bool
    reason: str = ""


def restore_security_state(
    store: SecurityStateStore,
    *,
    configured_mode: PrivacyMode,
    privacy: PrivacyService,
    killswitch: KillSwitchService,
) -> RestoreOutcome:
    """Apply the stored state to freshly built services. Never raises for an unreadable store."""
    try:
        stored = store.load()
    except SecurityStateUnreadableError as exc:
        return _restore_unreadable(
            exc.reason, exc.privacy_mode, configured_mode, privacy, killswitch
        )
    except Exception as exc:  # noqa: BLE001 - any read failure is "unreadable", never "empty"
        reason = f"{type(exc).__name__}: {exc}"
        return _restore_unreadable(reason, None, configured_mode, privacy, killswitch)
    if stored is None:
        log.info("security.state_fresh", privacy=configured_mode.value)
        return RestoreOutcome(source="configured", privacy_mode=configured_mode, kill_engaged=False)
    privacy.restore(stored.privacy_mode, panic=stored.panic)
    if stored.kill_engaged:
        killswitch.restore(
            origin=stored.kill_origin or "restored",
            reason=stored.kill_reason,
            security_path=stored.kill_security_path,
        )
    log.warning(
        "security.state_restored",
        privacy=stored.privacy_mode.value,
        panic=stored.panic,
        kill_engaged=stored.kill_engaged,
        security_path=stored.kill_security_path,
    )
    return RestoreOutcome(
        source="stored", privacy_mode=stored.privacy_mode, kill_engaged=stored.kill_engaged
    )


def _restore_unreadable(
    reason: str,
    stored_mode: PrivacyMode | None,
    configured_mode: PrivacyMode,
    privacy: PrivacyService,
    killswitch: KillSwitchService,
) -> RestoreOutcome:
    candidates = [configured_mode] if stored_mode is None else [configured_mode, stored_mode]
    mode = strictest_privacy_mode(*candidates)
    privacy.restore(mode, panic=False)
    killswitch.restore(
        origin=UNREADABLE_KILL_ORIGIN,
        reason=f"stored security state unreadable: {reason}",
        security_path=True,
    )
    log.critical(
        "security.state_unreadable",
        reason=reason,
        privacy=mode.value,
        note="booting in safe mode with the strictest privacy mode",
    )
    return RestoreOutcome(source="unreadable", privacy_mode=mode, kill_engaged=True, reason=reason)


class SecurityStateRecorder:
    """Writes the current snapshot after every change, in the order the changes happened.

    Each call snapshots the services *inside* the lock, so two changes in quick succession can
    never be written in the wrong order: whichever write runs last carries the latest state. The
    write itself runs in a thread, because the store is SQLite and this is called on the loop.
    """

    def __init__(
        self,
        store: SecurityStateStore,
        *,
        snapshot: Callable[[], PersistedSecurityState],
    ) -> None:
        self._store = store
        self._snapshot = snapshot
        self._lock = asyncio.Lock()
        #: Consecutive failed writes; health and tests read it.
        self.failures = 0

    async def record(self, reason: str) -> bool:
        """Persist the current state. False (and an error in the log) when the write failed."""
        async with self._lock:
            state = self._snapshot()
            try:
                await asyncio.to_thread(self._store.save, state)
            except Exception as exc:  # noqa: BLE001 - the in-memory state stands; say so loudly
                self.failures += 1
                log.error(
                    "security.state_persist_failed",
                    reason=reason,
                    error=f"{type(exc).__name__}: {exc}",
                    failures=self.failures,
                )
                return False
        self.failures = 0
        log.debug("security.state_persisted", reason=reason, privacy=state.privacy_mode.value)
        return True
