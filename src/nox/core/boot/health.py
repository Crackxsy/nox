"""The health checks the core owns.

Each one answers the same question in its own area: does this work right now, and if not, in words
the user can act on. A check never guesses - a component that cannot be asked reports
`unavailable` with the reason, not `available` by omission. Extensions add their own checks from
their `install(core)`; these are the ones every Nox has.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from nox.ai.base import AiProvider
from nox.core.boot.workers import WorkerSupervisor
from nox.core.events import HealthStatus
from nox.core.health import Check
from nox.data.db import Database
from nox.ipc.tokens import TokenStore

__all__ = ["core_health_checks"]

#: A model provider is allowed longer than the others: a cold local model or a cloud round-trip
#: legitimately takes seconds, and timing it out early would report a working provider as broken.
PROVIDER_PROBE_TIMEOUT_S = 15.0


def core_health_checks(
    *,
    database: Callable[[], Database | None],
    vault_dir: Callable[[], Path],
    workers: WorkerSupervisor,
    voice_enabled: bool,
    tokens: Callable[[], TokenStore | None],
    providers: Callable[[], list[AiProvider]],
) -> list[Check]:
    """Build the core's checks. Everything is read through a callable, because the components are
    built in order and a check may be created before the thing it asks about exists."""

    async def db_check() -> tuple[HealthStatus, str]:
        db = database()
        if db is None:
            return HealthStatus.UNAVAILABLE, "not opened"
        ok = await asyncio.to_thread(db.integrity_check)
        return (HealthStatus.AVAILABLE, "ok") if ok else (HealthStatus.UNAVAILABLE, "corrupt")

    async def vault_check() -> tuple[HealthStatus, str]:
        # The reason is a word, not the path: `/health` needs no authentication, and where the
        # user keeps their notes is not something an unauthenticated caller should learn. The path
        # itself is in the authenticated `/api/state`.
        exists = await asyncio.to_thread(vault_dir().exists)
        return (HealthStatus.AVAILABLE, "ok") if exists else (HealthStatus.UNAVAILABLE, "missing")

    async def voice_check() -> tuple[HealthStatus, str]:
        if not voice_enabled:
            return HealthStatus.UNAVAILABLE, "disabled"
        worker = workers.get("voice")
        if worker is None or (worker.process is not None and worker.process.poll() is not None):
            return HealthStatus.UNAVAILABLE, "worker not running"
        # Registered but not yet ready means the engines are still loading: `limited`, not
        # `available`, because nothing can be spoken yet.
        return (
            (HealthStatus.AVAILABLE, "worker registered")
            if worker.registered.is_set()
            else (HealthStatus.LIMITED, "worker starting")
        )

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

    checks = [
        Check("db", db_check),
        Check("vault", vault_check),
        Check("voice", voice_check),
        Check("tokens", tokens_check),
    ]
    for provider in providers():

        async def probe(p: AiProvider = provider) -> tuple[HealthStatus, str]:
            info = await p.health()
            return info.status, info.reason

        checks.append(Check(f"ai.{provider.info.id}", probe, timeout_s=PROVIDER_PROBE_TIMEOUT_S))
    return checks
