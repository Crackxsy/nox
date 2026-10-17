"""A secret store for machines without an OS keyring - a Raspberry Pi running Nox as a service.

The Windows Credential Manager, the macOS Keychain and the Secret Service on a Linux desktop all
need a logged-in user session. A headless Pi has none, and `keyring` then falls back to a backend
that refuses every call: the PIN could not be set, no calendar or Twitch token could be stored.

This store keeps the secrets in one JSON file in Nox's home folder, readable by the owning
account only (mode 0600, or an owner-only ACL on Windows). That is the same protection an SSH
private key has on the same machine, and it is stated as such - not dressed up with an
encryption whose key would have to sit right next to the file. `nox where` and the `secrets`
health check say when it is in use.

Safety properties, each because a reviewer found the gap:

* **No lost updates across processes.** The core, `nox secrets set` and the onboarding wizard are
  separate processes. Every read-modify-write holds an exclusive lock on `secrets.lock`.
* **No torn or redirected writes.** A fresh, uniquely named temporary file is created owner-only
  in the same folder, written, flushed to disk and renamed over the old one.
* **No foreign file is trusted.** `secrets.json` must be a regular file (not a symlink), owned by
  this user and unreadable to anyone else; otherwise the store refuses to read or overwrite it.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

from nox.ipc.tokens import restrict_to_owner
from nox.security._logging import get_logger
from nox.security.secret_names import SecretStoreError, validate_name

log = get_logger(__name__)

__all__ = ["LOCK_FILENAME", "SECRETS_FILENAME", "FileSecretStore"]

SECRETS_FILENAME = "secrets.json"
LOCK_FILENAME = "secrets.lock"
_FORMAT_VERSION = 1

#: One in-process lock per file, shared by every `FileSecretStore` pointing at it: the core may
#: open the store more than once, and the file lock below does not exclude threads of one process
#: on every platform.
_THREAD_LOCKS: dict[Path, threading.Lock] = {}

#: Never open the lock file through a planted link (0 where the flag does not exist).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(path, threading.Lock())


class FileSecretStore:
    """Secrets in `<home>/secrets.json`, owner-only. See the module docstring for the trade-off."""

    backend = "file"

    def __init__(self, home: Path) -> None:
        self._home = home
        self._path = home / SECRETS_FILENAME
        self._lock_path = home / LOCK_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def get(self, name: str) -> str | None:
        validate_name(name)
        if not self._path.exists():
            return None  # nothing stored yet: answer without creating the folder or a lock file
        with self._locked():
            return self._read().get(name)

    def set(self, name: str, value: str) -> None:
        validate_name(name)
        with self._locked():
            values = self._read()
            values[name] = value
            self._write(values)
        log.debug("secrets.set", name=name, backend="file")

    def delete(self, name: str) -> None:
        validate_name(name)
        with self._locked():
            values = self._read()
            if values.pop(name, None) is None:
                return
            self._write(values)
        log.debug("secrets.deleted", name=name, backend="file")

    def names(self) -> list[str]:
        if not self._path.exists():
            return []
        with self._locked():
            return sorted(self._read())

    # ---- locking ---------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with _thread_lock(self._path):
            self._ensure_home()
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW, 0o600)
            try:
                try:
                    _lock_fd(fd)
                except OSError as exc:  # Windows gives up after ~10 s of contention
                    raise SecretStoreError(f"{self._path} is locked by another process") from exc
                try:
                    yield
                finally:
                    _unlock_fd(fd)
            finally:
                os.close(fd)

    def _ensure_home(self) -> None:
        """The folder exists and is the owner's alone (0700 on POSIX; the ACL is per file there)."""
        if not self._home.exists():
            self._home.mkdir(parents=True, mode=0o700)
        if sys.platform == "win32":
            return
        info = os.stat(self._home)
        if info.st_uid != os.getuid():
            raise SecretStoreError(f"{self._home} belongs to another user; refusing to use it")
        if info.st_mode & 0o077:
            log.warning(
                "secrets.home_tightened",
                path=str(self._home),
                mode=f"{stat.S_IMODE(info.st_mode):o}",
                now="700",
            )
            os.chmod(self._home, 0o700)

    # ---- file handling ---------------------------------------------------------------------------

    def _read(self) -> dict[str, str]:
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            return {}
        self._check_trusted(info)
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SecretStoreError(f"{self._path} cannot be read: {type(exc).__name__}") from exc
        if not isinstance(data, dict) or data.get("version") != _FORMAT_VERSION:
            raise SecretStoreError(
                f"{self._path} is not a Nox secrets file (version {_FORMAT_VERSION})"
            )
        secrets = data.get("secrets", {})
        if not isinstance(secrets, dict):
            raise SecretStoreError(f"{self._path} has no secrets table")
        return {str(k): str(v) for k, v in secrets.items()}

    def _check_trusted(self, info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise SecretStoreError(f"{self._path} is not a regular file (a link?); refusing it")
        if sys.platform == "win32":
            return  # ownership and access are an ACL there, set by `restrict_to_owner` on write
        if info.st_uid != os.getuid():
            raise SecretStoreError(f"{self._path} belongs to another user; refusing it")
        if info.st_mode & 0o077:
            raise SecretStoreError(
                f"{self._path} is readable by others (mode {stat.S_IMODE(info.st_mode):o}); "
                "run: chmod 600 on it"
            )

    def _write(self, values: dict[str, str]) -> None:
        payload = json.dumps({"version": _FORMAT_VERSION, "secrets": values}, indent=1)
        # mkstemp creates a new file (O_EXCL) with mode 0600 on POSIX, so no existing file or
        # planted link is ever opened. On Windows the ACL goes on before any secret is written.
        fd, tmp_name = tempfile.mkstemp(dir=self._home, prefix=".secrets-", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            if not restrict_to_owner(tmp):
                raise SecretStoreError(f"could not restrict {tmp} to its owner; nothing written")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            tmp.unlink(missing_ok=True)
            raise
        _fsync_dir(self._home)

    def __repr__(self) -> str:
        return f"FileSecretStore(path={str(self._path)!r})"


# ---- platform helpers ----------------------------------------------------------------------------


def _lock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415 - Windows only

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        import fcntl  # noqa: PLC0415 - POSIX only

        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415

        fcntl.flock(fd, fcntl.LOCK_UN)


def _fsync_dir(directory: Path) -> None:
    """Make the rename itself durable (POSIX); Windows has no directory handles to sync."""
    if sys.platform == "win32":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
