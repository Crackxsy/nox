"""Session and one-time worker tokens for the IPC hub (IPC Model §Handshake, Process Model, ADR-3).

- The session token is generated once per core start and written to `<runtime>/session.token` with
  owner-only permissions (Windows: `icacls` removes inheritance and grants only the current user).
- Worker/plugin tokens are issued per spawn, delivered via the environment (`NOX_WORKER_TOKEN`),
  expire after a TTL and are consumed on first successful authentication.
- All comparisons are constant-time.
"""

from __future__ import annotations

import hmac
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nox.ipc._log import get_logger

log = get_logger(__name__)

SESSION_TOKEN_FILE = "session.token"  # noqa: S105 - a file name, not a secret
WORKER_TOKEN_ENV = "NOX_WORKER_TOKEN"  # noqa: S105 - an env var name, not a secret
DEFAULT_WORKER_TOKEN_TTL_S = 60.0

TokenKind = Literal["session", "worker"]
SESSION_ROLES: frozenset[str] = frozenset({"shell", "pet", "dashboard"})
WORKER_ROLES: frozenset[str] = frozenset({"worker", "plugin"})

_CREATE_NO_WINDOW = 0x08000000


def generate_token() -> str:
    """256 bits of randomness, URL-safe (fits in a query string and an env var)."""
    return secrets.token_urlsafe(32)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def restrict_to_owner(path: Path) -> bool:
    """Make `path` readable/writable by the current user only.

    Windows: `icacls <path> /inheritance:r /grant:r <DOMAIN\\user>:F` (pywin32 is not a dependency).
    POSIX: chmod 600. Returns True when the restriction was applied, False when it could not be
    (logged as a warning; the caller reports the token file as degraded in that case).
    """
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return True
    user = os.environ.get("USERNAME")
    if not user:
        log.warning("token_acl_skipped", reason="USERNAME not set")
        return False
    domain = os.environ.get("USERDOMAIN")
    principal = f"{domain}\\{user}" if domain else user
    cmd = ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, text=True, check=False, creationflags=_CREATE_NO_WINDOW
        )
    except OSError as exc:
        log.warning("token_acl_failed", error=str(exc))
        return False
    if result.returncode != 0:
        log.warning("token_acl_failed", returncode=result.returncode, stderr=result.stderr.strip())
        return False
    return True


def write_secret_file(path: Path, content: str) -> bool:
    """Create `path` owner-only, restrict its ACL, then write `content`. Returns ACL success."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    restricted = restrict_to_owner(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return restricted


def read_session_token(runtime_dir: Path) -> str:
    """Read the session token written by the core (used by the shell)."""
    token = (runtime_dir / SESSION_TOKEN_FILE).read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise ValueError("session token file is empty or too short")
    return token


@dataclass(frozen=True)
class WorkerToken:
    worker_id: str
    expires_at: float


@dataclass(frozen=True)
class AuthDecision:
    ok: bool
    kind: TokenKind | None = None
    reason: str = ""


class TokenStore:
    """Holds the per-start session token and outstanding one-time worker tokens."""

    def __init__(
        self,
        session_token: str | None = None,
        *,
        worker_ttl_s: float = DEFAULT_WORKER_TOKEN_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session_token or generate_token()
        self._worker_ttl_s = worker_ttl_s
        self._clock = clock
        self._worker: dict[str, WorkerToken] = {}
        self._session_file: Path | None = None
        self._session_file_restricted: bool | None = None

    @property
    def session_token(self) -> str:
        return self._session

    @property
    def session_file(self) -> Path | None:
        return self._session_file

    @property
    def session_file_restricted(self) -> bool | None:
        """None until written; False means the ACL could not be tightened (report as degraded)."""
        return self._session_file_restricted

    def write_session_token(self, runtime_dir: Path) -> Path:
        path = runtime_dir / SESSION_TOKEN_FILE
        self._session_file_restricted = write_secret_file(path, self._session)
        self._session_file = path
        log.info("session_token_written", path=str(path), restricted=self._session_file_restricted)
        return path

    def remove_session_token(self) -> None:
        if self._session_file is not None:
            self._session_file.unlink(missing_ok=True)
            self._session_file = None

    # -- worker tokens ------------------------------------------------------------

    def issue_worker_token(self, worker_id: str, *, ttl_s: float | None = None) -> str:
        """Issue a one-time token for a spawn of `worker_id` (e.g. "worker:stt")."""
        self.purge_expired()
        ttl = self._worker_ttl_s if ttl_s is None else ttl_s
        token = generate_token()
        self._worker[token] = WorkerToken(worker_id=worker_id, expires_at=self._clock() + ttl)
        log.debug("worker_token_issued", worker_id=worker_id, ttl_s=ttl)
        return token

    def worker_env(self, worker_id: str, *, ttl_s: float | None = None) -> Mapping[str, str]:
        """Environment additions for a worker spawn (never pass the token on the command line)."""
        return {WORKER_TOKEN_ENV: self.issue_worker_token(worker_id, ttl_s=ttl_s)}

    def outstanding_worker_tokens(self) -> int:
        self.purge_expired()
        return len(self._worker)

    def purge_expired(self) -> int:
        now = self._clock()
        expired = [t for t, meta in self._worker.items() if meta.expires_at <= now]
        for t in expired:
            del self._worker[t]
        return len(expired)

    # -- authentication -------------------------------------------------------------------------

    def authenticate(self, token: str, role: str, client_id: str) -> AuthDecision:
        """Check `token` for a connection claiming `role`/`client_id`; worker tokens are used up."""
        if role in SESSION_ROLES:
            if constant_time_equals(token, self._session):
                return AuthDecision(ok=True, kind="session")
            return AuthDecision(ok=False, reason="invalid token")
        if role in WORKER_ROLES:
            return self._authenticate_worker(token, client_id)
        return AuthDecision(ok=False, reason=f"role {role!r} cannot authenticate over the hub")

    def _authenticate_worker(self, token: str, client_id: str) -> AuthDecision:
        now = self._clock()
        matched: str | None = None
        # Scan every outstanding token with a constant-time compare (no early exit on match).
        for candidate in list(self._worker):
            if constant_time_equals(token, candidate):
                matched = candidate
        if matched is None:
            return AuthDecision(ok=False, reason="invalid token")
        meta = self._worker.pop(matched)  # consumed on first use, valid or not
        if meta.expires_at <= now:
            return AuthDecision(ok=False, reason="token expired")
        if meta.worker_id != client_id:
            return AuthDecision(ok=False, reason="token was issued for a different worker id")
        return AuthDecision(ok=True, kind="worker")
