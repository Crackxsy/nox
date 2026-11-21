"""`state.json` of a node - the versions, the pending one and the ones that failed - shared with
the launcher and changed only under a file lock, and `UpdateCheck`, the answer of one check."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from nox.release.verify import (
    VerifiedRelease,
)

#: A version that failed here is not tried again by itself for this long.
BAD_FOR_DAYS = 7


#: How long a state.lock is waited for (another launcher, node or CLI holds it).
LOCK_WAIT_S = 120.0


_VERSION_DIR = re.compile(
    r"^\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z]{1,20}(?:\.[0-9A-Za-z]{1,20})*)?$"
)


_CREATED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


Status = Literal[
    "up_to_date",
    "available",
    "needs_confirmation",
    "needs_installer",
    "refused",
    "not_configured",
    "installed",
]


Kind = Literal["", "patch", "minor", "major"]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@contextlib.contextmanager
def state_lock(root: Path) -> Iterator[None]:
    """An exclusive lock on `<root>/state.lock` for one read-modify-write of `state.json`. The
    launcher, the running node and `nox node update` all take it."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "state.lock").open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415

            handle.seek(0)
            pause, waited = 0.05, 0.0
            while True:  # LK_LOCK gives up after ~10 s: retry without a limit that short
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if waited >= LOCK_WAIT_S:
                        raise
                    time.sleep(pause)
                    waited += pause
                    pause = min(pause * 2, 1.0)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _version_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and _VERSION_DIR.match(value) else None


@dataclass
class UpdateState:
    """`<node home>/app/state.json`."""

    current: str | None = None
    previous: str | None = None
    pending: str | None = None
    #: Crashes of the pending version so far (the launcher counts them).
    failures: int = 0
    floor: str | None = None
    #: Version -> when it failed (UTC ISO); skipped for `BAD_FOR_DAYS`.
    bad: dict[str, str] = field(default_factory=dict)
    #: The `created` stamp of the newest signed manifest seen, and when it was first seen.
    newest_created: str | None = None
    newest_seen_at: str | None = None

    @classmethod
    def load(cls, path: Path) -> UpdateState:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        state = cls()
        for name in ("current", "previous", "pending", "floor"):
            setattr(state, name, _version_or_none(raw.get(name)))
        failures = raw.get("failures")
        state.failures = failures if isinstance(failures, int) and failures >= 0 else 0
        bad = raw.get("bad")
        if isinstance(bad, dict):
            state.bad = {k: v for k, v in bad.items() if _version_or_none(k) and isinstance(v, str)}
        for name in ("newest_created", "newest_seen_at"):
            value = raw.get(name)
            setattr(state, name, value if isinstance(value, str) and len(value) <= 40 else None)
        return state

    def save(self, path: Path) -> None:
        """Atomic: a reader sees the old or the new state, never half of one."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".new")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(asdict(self), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def is_bad(self, version: str, now: dt.datetime | None = None) -> bool:
        when = self.bad.get(version)
        if when is None:
            return False
        try:
            failed = dt.datetime.fromisoformat(when)
        except ValueError:
            return True
        return (now or _now()) - failed < dt.timedelta(days=BAD_FOR_DAYS)


@dataclass(frozen=True)
class UpdateCheck:
    status: Status
    message: str
    version: str = ""
    kind: Kind = ""
    release: VerifiedRelease | None = None
    manifest: bytes = b""
    signature: bytes = b""
