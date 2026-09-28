"""The health checks the core owns.

Each one answers the same question in its own area: does this work right now, and if not, in words
the user can act on. A check never guesses - a component that cannot be asked reports
`unavailable` with the reason, not `available` by omission. Extensions add their own checks from
their `install(core)`; these are the ones every Nox has.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from nox.ai.base import AiProvider, ProviderInfo
from nox.core.boot.persistence import OpenedDatabase
from nox.core.boot.voice_health import VoiceHealthReport
from nox.core.boot.workers import WorkerSupervisor
from nox.core.events import HealthStatus
from nox.core.health import Check
from nox.data.db import Database
from nox.ipc.tokens import TokenStore
from nox.security.model import SecretStore
from nox.security.secrets import PIN_SECRET_NAME, SecretStoreUnavailableError

__all__ = [
    "DB_INTEGRITY_CHECK_INTERVAL_S",
    "DB_QUICK_CHECK_INTERVAL_S",
    "DatabaseProbe",
    "core_health_checks",
]

#: A model provider is allowed longer than the others: a cold local model legitimately takes
#: seconds, and timing it out early would report a working provider as broken.
PROVIDER_PROBE_TIMEOUT_S = 15.0

#: How often the database gets the full `integrity_check` (boot does one as well).
DB_INTEGRITY_CHECK_INTERVAL_S = 24 * 3600.0

#: How often it gets the cheaper `quick_check` in between. Every other health round reports the
#: last result: both checks read the whole file under the connection lock.
DB_QUICK_CHECK_INTERVAL_S = 3600.0

ProviderHealth = Callable[[AiProvider], Awaitable[ProviderInfo]]


class DatabaseProbe:
    """The `db` check: scheduled integrity checks, and what boot had to do to open the file.

    Boot already ran the full check (`open_database`), so the schedule starts from there. A check
    that outlives the probe's timeout keeps running in its thread; the next rounds report the last
    result instead of stacking a second scan on top of it.
    """

    def __init__(
        self,
        database: Callable[[], Database | None],
        opened: Callable[[], OpenedDatabase | None] = lambda: None,
        *,
        clock: Callable[[], float] = time.monotonic,
        full_interval_s: float = DB_INTEGRITY_CHECK_INTERVAL_S,
        quick_interval_s: float = DB_QUICK_CHECK_INTERVAL_S,
    ) -> None:
        self._database = database
        self._opened = opened
        self._clock = clock
        self._full_interval_s = full_interval_s
        self._quick_interval_s = quick_interval_s
        started = clock()
        self._last_full = started
        self._last_quick = started
        self._ok = True
        self._running: asyncio.Future[bool] | None = None

    async def probe(self) -> tuple[HealthStatus, str]:
        db = self._database()
        if db is None:
            return HealthStatus.UNAVAILABLE, "not opened"
        running = self._running
        if running is not None:
            if not running.done():
                return self._status()  # an earlier, slow check is still reading the file
            self._running = None
            self._ok = running.result()
        check = self._due(db)
        if check is not None:
            self._running = asyncio.ensure_future(asyncio.to_thread(check))
            self._ok = await asyncio.shield(self._running)
            self._running = None
        return self._status()

    def _status(self) -> tuple[HealthStatus, str]:
        if not self._ok:
            return HealthStatus.UNAVAILABLE, "corrupt"
        return self._opened_status()

    def _due(self, db: Database) -> Callable[[], bool] | None:
        now = self._clock()
        if now - self._last_full >= self._full_interval_s:
            self._last_full = self._last_quick = now
            return db.integrity_check
        if now - self._last_quick >= self._quick_interval_s:
            self._last_quick = now
            return db.quick_check
        return None

    def _opened_status(self) -> tuple[HealthStatus, str]:
        opened = self._opened()
        if opened is not None and opened.recovery is not None:
            return HealthStatus.LIMITED, opened.recovery.describe()
        if opened is not None and opened.backup_error:
            return (
                HealthStatus.LIMITED,
                "migrated without a backup: the backup could not be written",
            )
        return HealthStatus.AVAILABLE, "ok"


def core_health_checks(
    *,
    database: Callable[[], Database | None],
    vault_dir: Callable[[], Path],
    workers: WorkerSupervisor,
    voice_enabled: bool,
    tokens: Callable[[], TokenStore | None],
    providers: Callable[[], list[AiProvider]],
    secrets: Callable[[], SecretStore | None] = lambda: None,
    database_probe: DatabaseProbe | None = None,
    provider_health: ProviderHealth | None = None,
    voice_report: Callable[[], VoiceHealthReport | None] = lambda: None,
) -> list[Check]:
    """Build the core's checks. Everything is read through a callable, because the components are
    built in order and a check may be created before the thing it asks about exists.

    `provider_health` is the router's cached, privacy-aware probe; without it a provider is asked
    directly (tests, and a core without a router)."""
    db_probe = database_probe or DatabaseProbe(database)

    async def vault_check() -> tuple[HealthStatus, str]:
        # The reason is a word, not the path: `/health` needs no authentication, and where the
        # user keeps their notes is not something an unauthenticated caller should learn. The path
        # itself is in the authenticated `/api/state`.
        exists = await asyncio.to_thread(vault_dir().exists)
        return (HealthStatus.AVAILABLE, "ok") if exists else (HealthStatus.UNAVAILABLE, "missing")

    async def voice_check() -> tuple[HealthStatus, str]:
        if not voice_enabled:
            return HealthStatus.UNAVAILABLE, "disabled"
        # The process first (running, restarting, given up - with the exit reason), then, once it
        # is ready, what the worker itself reports: a silent microphone, a missing model, text-only
        # wake word or no echo cancellation is the answer, not the fact that it connected.
        status, reason = workers.health("voice")
        if status is not HealthStatus.AVAILABLE:
            return status, reason
        report = voice_report()
        summary = report.summary() if report is not None else None
        return summary or (status, reason)

    async def tokens_check() -> tuple[HealthStatus, str]:
        # The session token file is a secret on disk. When its permissions could not be tightened,
        # health says so, rather than leaving the promise in a docstring.
        store = tokens()
        if store is None:
            return HealthStatus.UNAVAILABLE, "no session token"
        restricted = store.session_file_restricted
        if restricted is None:
            return HealthStatus.LIMITED, "session token not written yet"
        if restricted:
            return HealthStatus.AVAILABLE, "owner-only"
        return HealthStatus.LIMITED, "could not restrict the token file to this account"

    async def secrets_check() -> tuple[HealthStatus, str]:
        # One read of a known entry answers "can Nox reach the credential store at all". The value
        # never leaves this function; only whether the read worked does.
        store = secrets()
        if store is None:
            return HealthStatus.UNAVAILABLE, "not built"
        try:
            await asyncio.to_thread(store.get, PIN_SECRET_NAME)
        except SecretStoreUnavailableError as exc:
            return HealthStatus.UNAVAILABLE, str(exc)
        return HealthStatus.AVAILABLE, "ok"

    checks = [
        Check("db", db_probe.probe),
        Check("vault", vault_check),
        Check("voice", voice_check),
        Check("tokens", tokens_check),
        Check("secrets", secrets_check),
    ]
    for provider in providers():

        async def probe(p: AiProvider = provider) -> tuple[HealthStatus, str]:
            info = await (provider_health(p) if provider_health is not None else p.health())
            return info.status, info.reason

        checks.append(Check(f"ai.{provider.info.id}", probe, timeout_s=PROVIDER_PROBE_TIMEOUT_S))
    return checks
