"""A node updates itself - only from a signed release, never from its core (spec C §1.9).

About once a day the node fetches `release-manifest.json` and its signature from the project's
latest GitHub release and decides, in this order. Any "no" stops it and changes nothing:

1. **A release key is trusted** by the version that runs here (`nox.release.trusted_keys`).
   None yet: "release signing is not configured", no update at all.
2. **The signature** is valid Ed25519 by a trusted, unexpired key over the manifest's exact bytes
   (`nox.release.verify.verify_release`: unknown key, bad signature, expired key are refusals).
3. **The manifest** is well-formed and names this project.
4. **No downgrade, no replay:** its version is not below the running one nor below the highest
   version that ran here, and it was not created before the newest manifest this node has
   already seen (an old, validly signed manifest cannot be replayed to freeze the node). Equal
   version: up to date. A version that failed to start here is skipped for `BAD_FOR_DAYS`.
5. **Policy:** only the `stable` channel installs by itself, only a patch, only when the release
   changed no library (`deps_changed: false`), and only when the *paired core* accepts the
   protocol the *new* node speaks (`node_speaks` in the manifest, within the range the core sent
   at the last connection). Anything else waits for `nox node update` on the device - or, when
   the libraries changed, for the installer.
6. **The bundle** `nox-app-<tag>.zip` (from that tag's own release) is listed in the signed
   manifest, downloaded with a size cap and matches the listed size and **SHA-256** - hashed and
   unpacked from one open file.
7. **Its contents** (`nox.node.bundle`): only listed, regular, bounded files under `nox/` and
   `config/`, each with the SHA-256 from `BUNDLE.json`; the same version, the protocol the
   manifest names, and the `uv.lock` of the running code.
8. **A trial start:** the new code is imported with this Python, isolated, and reports its
   version.

Then one atomic replace of `state.json` (under a file lock shared with the launcher) makes the
new version `pending`, and the node restarts through its launcher (`installer/start_node.pyw`),
which checks the folder again against the signed manifest before it runs it. The new version
becomes `current` once it connected to its core *and* the core accepted its protocol; a start
that crashes `MAX_TRIALS` times, or a core that refuses its protocol, rolls it back.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from nox import __version__
from nox.node.bundle import ARCHIVE_NAME, BundleError, bundle_name, sha256_of, unpack
from nox.release import trusted_release_keys
from nox.release.keys import TrustedKey
from nox.release.manifest import MANIFEST_NAME, MAX_MANIFEST_BYTES, SIGNATURE_NAME, Version
from nox.release.verify import (
    MAX_SIGNATURE_BYTES,
    ReleaseVerificationError,
    SigningNotConfiguredError,
    VerifiedRelease,
    verify_release,
)

__all__ = [
    "BAD_FOR_DAYS",
    "RESTART_EXIT_CODE",
    "STALE_AFTER_DAYS",
    "TRIAL_FAILED_EXIT_CODE",
    "HttpsFetcher",
    "NodeUpdater",
    "UpdateCheck",
    "UpdateState",
    "bundle_name",
    "python_env",
    "state_lock",
]

RELEASES_URL = "https://github.com/Crackxsy/nox/releases/"
#: `nox node run` ends with this when it should start again (a new version is in place).
RESTART_EXIT_CODE = 75
#: ... and with this when its core refused the protocol of a version on trial: roll it back.
TRIAL_FAILED_EXIT_CODE = 76
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
#: A version that failed here is not tried again by itself for this long.
BAD_FOR_DAYS = 7
#: No newer signed manifest for this long: say so (a frozen mirror, or simply no releases).
STALE_AFTER_DAYS = 30
_VERSION_DIR = re.compile(
    r"^\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z]{1,20}(?:\.[0-9A-Za-z]{1,20})*)?$"
)
_PATH = re.compile(r"^(?:latest/download|download/v[0-9A-Za-z.-]{1,60})/[A-Za-z0-9._+-]{1,200}$")

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


# ---- fetching -----------------------------------------------------------------------------------


class Fetcher(Protocol):
    def get(self, path: str, limit: int) -> bytes: ...
    def download(self, path: str, dest: Path, limit: int) -> None: ...


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """GitHub answers release downloads with a redirect to its file host; never to plain HTTP."""

    def redirect_request(  # noqa: PLR0913 - the standard library's signature
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code, "refused: redirect away from HTTPS", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpsFetcher:
    """Release files over HTTPS with the system's certificate store, size-capped. Paths are
    `latest/download/<file>` or `download/<tag>/<file>` below the project's releases."""

    def __init__(self, base: str = RELEASES_URL, *, timeout_s: float = 60.0) -> None:
        if not base.startswith("https://"):
            raise ValueError("release files only come over HTTPS")
        self._base = base
        self._timeout = timeout_s
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), _HttpsOnly()
        )

    def _open(self, path: str) -> Any:
        if not _PATH.match(path):
            raise ValueError(f"not a release file path: {path[:80]!r}")
        return self._opener.open(self._base + path, timeout=self._timeout)

    def get(self, path: str, limit: int) -> bytes:
        with self._open(path) as response:
            data: bytes = response.read(limit + 1)
        if len(data) > limit:
            raise ReleaseVerificationError("too_large", f"{path} is larger than expected")
        return data

    def download(self, path: str, dest: Path, limit: int) -> None:
        written = 0
        with self._open(path) as response, dest.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise ReleaseVerificationError("too_large", f"{path} is larger than listed")
                out.write(chunk)


# ---- state.json (shared with the launcher) ------------------------------------------------------


@contextlib.contextmanager
def state_lock(root: Path) -> Iterator[None]:
    """An exclusive lock on `<root>/state.lock` for one read-modify-write of `state.json`. The
    launcher, the running node and `nox node update` all take it."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "state.lock").open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
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


def python_env(**extra: str) -> dict[str, str]:
    """The environment for a Python this node starts: without any PYTHON* variable (no
    PYTHONPATH, PYTHONHOME, PYTHONSTARTUP ... can redirect what it imports), plus `extra`.
    Together with `-I` (isolated: no current folder, no user site, no PYTHON* variables) and a
    trusted working folder, only the code that was chosen runs."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env.update(extra)
    return env


def running_lock_sha256() -> str | None:
    """The `uv.lock` the running code belongs to: from its own `BUNDLE.json` (a self-installed
    version), the checkout's `uv.lock` (Linux, macOS), or `uv.lock.sha256` (the Windows
    payload). None when unknown - then only the installer updates."""
    from nox.paths import REPO_ROOT  # noqa: PLC0415

    with contextlib.suppress(OSError, ValueError, AttributeError):
        info = json.loads((REPO_ROOT / "BUNDLE.json").read_text(encoding="utf-8"))
        value = info.get("lock_sha256")
        if isinstance(value, str):
            return value
    with contextlib.suppress(OSError):
        return hashlib.sha256((REPO_ROOT / "uv.lock").read_bytes()).hexdigest()
    with contextlib.suppress(OSError):
        return (REPO_ROOT / "uv.lock.sha256").read_text(encoding="ascii").strip()
    return None


def _default_smoke(app: Path, python: str) -> str:
    """Import the new code with this Python, isolated, in a process of its own; its version."""
    code = (
        "import os, sys; sys.path.insert(0, os.environ['NOX_NODE_APP']); "
        "import nox, nox.cli, nox.node.agent, nox.node.update; print(nox.__version__)"
    )
    result = subprocess.run(  # noqa: S603 - this interpreter, fixed code; the folder is data
        [python, "-I", "-B", "-c", code],
        env=python_env(NOX_NODE_APP=str(app)),
        cwd=str(app),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _refused(code: str, detail: object, version: str = "") -> UpdateCheck:
    return UpdateCheck("refused", f"update refused ({code}): {str(detail)[:300]}", version)


class NodeUpdater:
    def __init__(
        self,
        root: Path,
        *,
        fetcher: Fetcher | None = None,
        trusted_keys: Callable[[], Sequence[TrustedKey]] = trusted_release_keys,
        running: str = __version__,
        core_range: Callable[[], tuple[int, int] | None] = lambda: None,
        python: str = sys.executable,
        smoke: Callable[[Path, str], str] = _default_smoke,
        lock_sha256: Callable[[], str | None] = running_lock_sha256,
        clock: Callable[[], dt.datetime] = _now,
    ) -> None:
        self.root = root
        self._fetcher = fetcher or HttpsFetcher()
        self._keys = trusted_keys
        self._running = Version.parse(running)
        self._core_range = core_range
        self._python = python
        self._smoke = smoke
        self._lock = lock_sha256
        self._clock = clock

    state_file = property(lambda self: self.root / "state.json")
    releases = property(lambda self: self.root / "releases")

    def state(self) -> UpdateState:
        return UpdateState.load(self.state_file)

    @contextlib.contextmanager
    def _editing(self) -> Iterator[UpdateState]:
        with state_lock(self.root):
            state = self.state()
            yield state
            state.save(self.state_file)

    # -- deciding -----------------------------------------------------------------------------

    def check(self, *, retry_bad: bool = False) -> UpdateCheck:
        """What the newest signed release means for this node. Never raises."""
        try:
            return self._check(retry_bad)
        except Exception as exc:  # noqa: BLE001 - a failed check is a refusal, never a crash
            return _refused(type(exc).__name__, exc)

    def _check(self, retry_bad: bool) -> UpdateCheck:
        keys = self._keys()
        if not keys:
            return UpdateCheck("not_configured", str(SigningNotConfiguredError()))
        state = self.state()
        floor = self._running
        if state.floor and Version.parse(state.floor) > floor:
            floor = Version.parse(state.floor)
        try:
            manifest = self._fetcher.get(f"latest/download/{MANIFEST_NAME}", MAX_MANIFEST_BYTES)
            signature = self._fetcher.get(f"latest/download/{SIGNATURE_NAME}", MAX_SIGNATURE_BYTES)
            release = verify_release(manifest, signature, trusted_keys=keys, min_version=str(floor))
        except ReleaseVerificationError as exc:
            return _refused(exc.code, exc)
        except OSError as exc:
            return _refused("unreachable", type(exc).__name__)
        found = release.manifest
        # Freshness: a validly signed but older manifest is a replay (or a frozen mirror).
        if state.newest_created and found.created < state.newest_created:
            return _refused(
                "stale",
                f"the release information ({found.created}) is older than what this node has "
                f"already seen ({state.newest_created})",
            )
        if found.created != state.newest_created:
            with self._editing() as editing:
                editing.newest_created = found.created
                editing.newest_seen_at = self._clock().isoformat()
        version = found.version
        if version <= self._running:
            return UpdateCheck("up_to_date", f"Nox {self._running} is up to date.")
        name = str(version)
        if state.is_bad(name, self._clock()) and not retry_bad:
            return _refused(
                "failed_here",
                f"{name} failed to start here; it is tried again in {BAD_FOR_DAYS} days or "
                "with `nox node update --yes`",
                name,
            )
        kind: Kind = "patch"
        if version.major != self._running.major:
            kind = "major"
        elif version.minor != self._running.minor:
            kind = "minor"

        def result(status: Status, message: str) -> UpdateCheck:
            return UpdateCheck(status, message, name, kind, release, manifest, signature)

        if found.deps_changed is not False:
            return result(
                "needs_installer",
                f"Nox {name} changes libraries (or does not say): it needs the installer.",
            )
        reasons = []
        if found.channel != "stable":
            reasons.append("it is a pre-release")
        if kind != "patch":
            reasons.append(f"it is a {kind} update")
        span = self._core_range()
        if found.node_speaks is None or span is None:
            reasons.append("it is not known whether the paired core accepts it")
        elif not span[0] <= found.node_speaks <= span[1]:
            reasons.append(
                f"it speaks node protocol {found.node_speaks}, the paired core accepts "
                f"{span[0]}-{span[1]}"
            )
        if reasons:
            return result(
                "needs_confirmation",
                f"Nox {name} is available ({'; '.join(reasons)}): run `nox node update` on "
                "this device.",
            )
        return result("available", f"Nox {name} is available.")

    def stale_days(self) -> int | None:
        """Days since a newer signed manifest was last seen, when more than `STALE_AFTER_DAYS`."""
        seen = self.state().newest_seen_at
        if not seen:
            return None
        try:
            days = (self._clock() - dt.datetime.fromisoformat(seen)).days
        except ValueError:
            return None
        return days if days > STALE_AFTER_DAYS else None

    # -- installing ---------------------------------------------------------------------------

    def apply(self, check: UpdateCheck, *, confirmed: bool = False) -> UpdateCheck:
        """Download, verify, unpack, trial-start and switch. On any failure nothing is switched.
        Never raises."""
        allowed = check.status == "available" or (
            check.status == "needs_confirmation" and confirmed
        )
        if not allowed or check.release is None:
            return check
        version = str(check.release.manifest.version)
        staging = self.releases / f"{version}.staging"
        try:
            return self._apply(check, check.release, staging)
        except (ReleaseVerificationError, BundleError) as exc:
            return _refused(getattr(exc, "code", "bad_bundle"), exc, version)
        except Exception as exc:  # noqa: BLE001 - network, disk, zip: a refusal, never a crash
            return _refused(type(exc).__name__, exc, version)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _apply(self, check: UpdateCheck, release: VerifiedRelease, staging: Path) -> UpdateCheck:
        manifest = release.manifest
        version = str(manifest.version)
        name = bundle_name(manifest.tag)
        asset = manifest.asset(name)
        if asset is None:
            return _refused("no_bundle", f"{manifest.tag} has no node bundle; use the installer")
        if asset.size > MAX_BUNDLE_BYTES:
            return _refused("too_large", f"{name} is larger than a node bundle can be")
        downloads = self.root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        archive = downloads / (name + ".part")
        shutil.rmtree(staging, ignore_errors=True)
        try:
            # From that tag's own release: "latest" may have moved on since the manifest.
            self._fetcher.download(f"download/{manifest.tag}/{name}", archive, asset.size)
            with archive.open("rb") as handle:  # hashed and unpacked from the same open file
                if sha256_of(handle) != (asset.sha256, asset.size):
                    raise ReleaseVerificationError(
                        "mismatch", f"{name} does not match the size and SHA-256 of the manifest"
                    )
                handle.seek(0)
                with zipfile.ZipFile(handle) as bundle:
                    info = unpack(bundle, staging)
                handle.seek(0)
                with (staging / ARCHIVE_NAME).open("wb") as kept:
                    shutil.copyfileobj(handle, kept)
        finally:
            archive.unlink(missing_ok=True)
        problem = self._fits(info, version, manifest.node_speaks)
        if problem:
            return UpdateCheck("needs_installer", f"Nox {version}: {problem}", version)
        (staging / MANIFEST_NAME).write_bytes(check.manifest)
        (staging / SIGNATURE_NAME).write_bytes(check.signature)
        reported = self._smoke(staging, self._python)
        if reported != version:
            raise ReleaseVerificationError(
                "trial_start", f"the new version did not start here ({reported[:40] or '-'})"
            )
        with self._editing() as state:
            if version in {state.current, state.previous} or version == str(self._running):
                return _refused("in_use", f"{version} is already installed here", version)
            final = self.releases / version
            shutil.rmtree(final, ignore_errors=True)
            os.replace(staging, final)
            state.pending, state.failures = version, 0
            state.bad.pop(version, None)  # a deliberate retry starts over
        self._prune()
        return UpdateCheck(
            "installed", f"Nox {version} is installed; restarting.", version, check.kind
        )

    def _fits(self, info: dict[str, Any], version: str, speaks: int | None) -> str:
        if info["version"] != version:
            return f"the bundle is version {str(info['version'])[:20]}"
        if speaks is not None and info["node_protocol"] != speaks:
            return "the bundle speaks another protocol than its signed manifest says"
        lock = self._lock()
        if lock is None or info["lock_sha256"] != lock:
            return "it was built for other library versions than these: run the installer"
        return ""

    def _prune(self) -> None:
        """Remove release folders nothing points to - never the running, current, previous or
        pending one; only direct children with a version name."""
        with state_lock(self.root):
            state = self.state()
            keep = {state.current, state.previous, state.pending, str(self._running)}
            with contextlib.suppress(OSError):
                for folder in self.releases.iterdir():
                    if (
                        not folder.is_symlink()
                        and _VERSION_DIR.match(folder.name)
                        and folder.name not in keep
                    ):
                        shutil.rmtree(folder, ignore_errors=True)

    # -- after the restart --------------------------------------------------------------------

    def confirm_running(self, version: str = __version__) -> bool:
        """The pending version runs and its core accepted it: it becomes current, and no older
        release is accepted from now on."""
        with self._editing() as state:
            if state.pending != version:
                return False
            state.previous, state.current = state.current, version
            state.pending, state.failures = None, 0
            if state.floor is None or Version.parse(state.floor) < Version.parse(version):
                state.floor = version
        return True

    def on_trial(self, running: str = __version__) -> bool:
        return self.state().pending == running

    def restart_needed(self, running: str = __version__) -> bool:
        """A version other than the running one waits to start (installed by `nox node update`)."""
        return self.state().pending not in (None, running)
