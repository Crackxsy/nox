"""A node updates itself - only from a signed release, never from its core (spec C §1.9).

Once a day the node fetches `release-manifest.json` and its signature from the project's latest
GitHub release and decides, in this order - every "no" stops it, nothing is changed:

1. **A release key is trusted** by the version that runs here (`nox.release.trusted_keys`).
   None yet: "release signing is not configured", no update at all - an unsigned or unverifiable
   manifest is never a reason to install anything.
2. **The signature** is valid Ed25519 by a trusted, unexpired key, over the manifest's exact bytes
   (`nox.release.verify.verify_release`; unknown key, bad signature, expired key are refusals).
3. **The manifest** is well-formed and names this project.
4. **No downgrade:** its version is not below the highest version this node ever ran successfully,
   nor below the running one (rollback protection). Equal: up to date. A version that failed to
   start here before is skipped.
5. **Patch or more:** a patch release that still speaks this node's protocol (`node_protocol`)
   installs by itself; a minor or major one waits until somebody runs `nox node update` here.
6. **The bundle** `nox-app-<tag>.zip` is listed in the signed manifest, downloaded with a size cap
   and must match the manifest's size and **SHA-256** exactly.
7. **Its contents:** only `nox/`, `config/` and `BUNDLE.json`, no links, no `..`, bounded size;
   `BUNDLE.json` names the same version, and the dependency versions it was built with are the
   ones installed here (otherwise the installer is needed - the bundle carries code, not
   libraries).
8. **A trial start:** the new code is imported with this Python and must report its version.

Then the switch is one atomic file replace (`state.json`: the new version becomes `pending`), and
the node restarts through its launcher (`installer/start_node.pyw`). The new version is
`current` only once it connected to the core or ran for `HEALTHY_AFTER_S`; a start that ends
before that rolls back to the previous version by itself, and that version is never tried again.
The core only ever *tells* a node that an update exists; it never sends code.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from nox import __version__
from nox.node.protocol import NODE_PROTOCOL
from nox.release import trusted_release_keys
from nox.release.keys import TrustedKey
from nox.release.manifest import MANIFEST_NAME, MAX_MANIFEST_BYTES, SIGNATURE_NAME, Version
from nox.release.verify import (
    MAX_SIGNATURE_BYTES,
    ReleaseVerificationError,
    SigningNotConfiguredError,
    VerifiedRelease,
    verify_file,
    verify_release,
)

__all__ = [
    "BUNDLE_SCHEMA",
    "HEALTHY_AFTER_S",
    "RESTART_EXIT_CODE",
    "HttpsFetcher",
    "NodeUpdater",
    "UpdateCheck",
    "UpdateState",
    "bundle_name",
]

RELEASES_URL = "https://github.com/Crackxsy/nox/releases/latest/download/"
BUNDLE_SCHEMA = "nox-app-bundle/1"
#: `nox node run` ends with this when it should start again (a new version is in place).
RESTART_EXIT_CODE = 75
#: A new version that ran this long (or connected to its core) counts as working.
HEALTHY_AFTER_S = 120.0
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 5000
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.+ -]*"
_ENTRY = re.compile(rf"^(?:BUNDLE\.json|(?:nox|config)/{_SEGMENT}(?:/{_SEGMENT})*)$")
#: Names Windows treats as devices whatever the extension, and names it silently changes.
_WINDOWS_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com\d|lpt\d)(?:\..*)?$|[. ]$", re.IGNORECASE)
_VERSION_DIR = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z.]{1,40})?$")

Status = Literal[
    "up_to_date", "available", "needs_confirmation", "refused", "not_configured", "installed"
]


def bundle_name(tag: str) -> str:
    return f"nox-app-{tag}.zip"


class Fetcher(Protocol):
    def get(self, name: str, limit: int) -> bytes: ...
    def download(self, name: str, dest: Path, limit: int) -> None: ...


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
    """Release files over HTTPS with the system's certificate store, size-capped."""

    def __init__(self, base: str = RELEASES_URL, *, timeout_s: float = 60.0) -> None:
        if not base.startswith("https://"):
            raise ValueError("release files only come over HTTPS")
        self._base = base
        self._timeout = timeout_s
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), _HttpsOnly()
        )

    def _open(self, name: str) -> Any:
        return self._opener.open(self._base + name, timeout=self._timeout)

    def get(self, name: str, limit: int) -> bytes:
        with self._open(name) as response:
            data: bytes = response.read(limit + 1)
        if len(data) > limit:
            raise ReleaseVerificationError("too_large", f"{name} is larger than expected")
        return data

    def download(self, name: str, dest: Path, limit: int) -> None:
        written = 0
        with self._open(name) as response, dest.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise ReleaseVerificationError("too_large", f"{name} is larger than listed")
                out.write(chunk)


@dataclass
class UpdateState:
    """`<node home>/app/state.json` - shared with the launcher, which reads and rolls back."""

    current: str | None = None
    previous: str | None = None
    pending: str | None = None
    attempts: int = 0
    floor: str | None = None
    bad: list[str] = field(default_factory=list)

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
            value = raw.get(name)
            setattr(
                state, name, value if isinstance(value, str) and _VERSION_DIR.match(value) else None
            )
        attempts = raw.get("attempts")
        state.attempts = attempts if isinstance(attempts, int) else 0
        bad = raw.get("bad")
        state.bad = [v for v in bad if isinstance(v, str)][:50] if isinstance(bad, list) else []
        return state

    def save(self, path: Path) -> None:
        """Atomic: the launcher sees the old or the new state, never half of one."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".new")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(asdict(self), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


@dataclass(frozen=True)
class UpdateCheck:
    status: Status
    message: str
    version: str = ""
    kind: Literal["", "patch", "minor", "major"] = ""
    release: VerifiedRelease | None = None


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def python_env(**extra: str) -> dict[str, str]:
    """The environment for a Python this node starts: without any PYTHON* variable (no
    PYTHONPATH, PYTHONHOME, PYTHONSTARTUP ... can redirect what it imports), plus `extra`.
    Together with `-I` (isolated: no current folder, no user site, no PYTHON* variables) and a
    trusted working folder, only the code that was chosen runs."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env.update(extra)
    return env


def _default_smoke(app: Path, python: str) -> str:
    """Import the new code with this Python, in a process of its own; its version, or ""."""
    code = (
        "import os, sys; sys.path.insert(0, os.environ['NOX_NODE_APP']); "
        "import nox, nox.cli, nox.node.agent, nox.node.update; print(nox.__version__)"
    )
    result = subprocess.run(  # noqa: S603 - this interpreter, fixed code; the folder is data
        [python, "-I", "-c", code],
        env=python_env(NOX_NODE_APP=str(app)),
        cwd=str(app),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


class NodeUpdater:
    def __init__(
        self,
        root: Path,
        *,
        fetcher: Fetcher | None = None,
        trusted_keys: Callable[[], Sequence[TrustedKey]] = trusted_release_keys,
        running: str = __version__,
        protocol: int = NODE_PROTOCOL,
        python: str = sys.executable,
        smoke: Callable[[Path, str], str] = _default_smoke,
        installed: Callable[[str], str | None] = installed_version,
    ) -> None:
        self.root = root
        self._fetcher = fetcher or HttpsFetcher()
        self._keys = trusted_keys
        self._running = Version.parse(running)
        self._protocol = protocol
        self._python = python
        self._smoke = smoke
        self._installed = installed

    state_file = property(lambda self: self.root / "state.json")
    releases = property(lambda self: self.root / "releases")

    def state(self) -> UpdateState:
        return UpdateState.load(self.state_file)

    # -- deciding -----------------------------------------------------------------------------

    def check(self) -> UpdateCheck:
        keys = self._keys()
        if not keys:
            return UpdateCheck("not_configured", str(SigningNotConfiguredError()))
        state = self.state()
        floor = self._running
        if state.floor and Version.parse(state.floor) > floor:
            floor = Version.parse(state.floor)
        try:
            manifest = self._fetcher.get(MANIFEST_NAME, MAX_MANIFEST_BYTES)
            signature = self._fetcher.get(SIGNATURE_NAME, MAX_SIGNATURE_BYTES)
            release = verify_release(manifest, signature, trusted_keys=keys, min_version=str(floor))
        except ReleaseVerificationError as exc:
            return UpdateCheck("refused", f"update refused ({exc.code}): {exc}")
        except OSError as exc:
            return UpdateCheck("refused", f"release not reachable: {type(exc).__name__}")
        found = release.manifest.version
        if found <= self._running:
            return UpdateCheck("up_to_date", f"Nox {self._running} is up to date.")
        if str(found) in state.bad:
            return UpdateCheck("refused", f"{found} failed to start here before; not again")
        if (found.major, found.minor) != (self._running.major, self._running.minor):
            kind: Literal["minor", "major"] = (
                "major" if found.major != self._running.major else "minor"
            )
            return UpdateCheck(
                "needs_confirmation",
                f"Nox {found} is available ({kind} update): run `nox node update` on this device.",
                str(found),
                kind,
                release,
            )
        span = release.manifest.node_protocol
        if span is None or not span[0] <= self._protocol <= span[1]:
            return UpdateCheck(
                "needs_confirmation",
                f"Nox {found} speaks another node protocol: run `nox node update` here.",
                str(found),
                "patch",
                release,
            )
        return UpdateCheck("available", f"Nox {found} is available.", str(found), "patch", release)

    # -- installing ---------------------------------------------------------------------------

    def apply(self, check: UpdateCheck, *, confirmed: bool = False) -> UpdateCheck:
        """Download, verify, unpack, trial-start and switch. On any failure nothing is switched."""
        allowed = check.status == "available" or (
            check.status == "needs_confirmation" and confirmed
        )
        if not allowed or check.release is None:
            return check
        release = check.release
        version = str(release.manifest.version)
        name = bundle_name(release.manifest.tag)
        asset = release.manifest.asset(name)
        if asset is None:
            return UpdateCheck(
                "refused", f"{release.manifest.tag} has no node bundle; use the installer"
            )
        if asset.size > MAX_BUNDLE_BYTES:
            return UpdateCheck("refused", f"{name} is larger than a node bundle can be")
        downloads = self.root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        archive = downloads / (name + ".part")
        staging = self.releases / f"{version}.staging"
        try:
            self._fetcher.download(name, archive, asset.size)
            verify_file(release, archive, name=name)  # size and SHA-256 from the signed manifest
            shutil.rmtree(staging, ignore_errors=True)
            self._unpack(archive, staging)
            problem = self._compatible(staging, version)
            if problem:
                raise ReleaseVerificationError("incompatible", problem)
            reported = self._smoke(staging, self._python)
            if reported != version:
                raise ReleaseVerificationError(
                    "trial_start", f"the new version did not start here ({reported or 'no answer'})"
                )
        except (
            ReleaseVerificationError,
            OSError,
            zipfile.BadZipFile,
            subprocess.SubprocessError,
        ) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            code = getattr(exc, "code", type(exc).__name__)
            return UpdateCheck("refused", f"update to {version} refused ({code}): {exc}", version)
        finally:
            archive.unlink(missing_ok=True)
        final = self.releases / version
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
        state = self.state()
        state.pending, state.attempts = version, 0
        state.save(self.state_file)  # the switch: one atomic replace
        self._prune(state)
        return UpdateCheck(
            "installed", f"Nox {version} is installed; restarting.", version, check.kind
        )

    def _unpack(self, archive: Path, target: Path) -> None:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ReleaseVerificationError("bad_bundle", "too many files in the bundle")
            if sum(e.file_size for e in entries) > MAX_UNPACKED_BYTES:
                raise ReleaseVerificationError("bad_bundle", "the bundle unpacks too large")
            for entry in entries:
                if entry.is_dir():
                    continue
                mode = entry.external_attr >> 16
                if (
                    not _ENTRY.match(entry.filename)
                    or "/../" in f"/{entry.filename}/"
                    or any(_WINDOWS_RESERVED.search(part) for part in entry.filename.split("/"))
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                ):
                    raise ReleaseVerificationError(
                        "bad_bundle", f"unexpected entry {entry.filename[:80]!r}"
                    )
                destination = target / entry.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with bundle.open(entry) as source, destination.open("wb") as out:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > entry.file_size:
                            raise ReleaseVerificationError(
                                "bad_bundle", "an entry is larger than listed"
                            )
                        out.write(chunk)

    def _compatible(self, app: Path, version: str) -> str:
        """Empty when the bundle runs on this installation's libraries, else why not."""
        try:
            info = json.loads((app / "BUNDLE.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "the bundle has no BUNDLE.json"
        if not isinstance(info, dict) or info.get("schema") != BUNDLE_SCHEMA:
            return "unknown bundle format"
        if info.get("version") != version:
            return f"the bundle is version {str(info.get('version'))[:20]}, not {version}"
        if (
            not (app / "nox" / "__init__.py").is_file()
            or not (app / "config" / "defaults.yaml").is_file()
        ):
            return "the bundle is incomplete"
        packages = info.get("packages")
        requires = info.get("requires")
        if not isinstance(packages, dict) or not isinstance(requires, list):
            return "the bundle lists no dependencies"
        for name, wanted in packages.items():
            have = self._installed(str(name))
            if have is not None and (not isinstance(wanted, list) or have not in wanted):
                return f"needs {name} {wanted}, this installation has {have}: run the installer"
        for name in requires:
            if self._installed(str(name)) is None:
                return f"needs {name}, which this installation lacks: run the installer"
        return ""

    def _prune(self, state: UpdateState) -> None:
        keep = {state.current, state.previous, state.pending}
        with contextlib.suppress(OSError):
            for folder in self.releases.iterdir():
                if folder.name not in keep:
                    shutil.rmtree(folder, ignore_errors=True)

    # -- after the restart --------------------------------------------------------------------

    def confirm_running(self, version: str = __version__) -> bool:
        """The pending version runs and works (it connected, or ran `HEALTHY_AFTER_S`): it
        becomes current, and no older release is accepted from now on."""
        state = self.state()
        if state.pending != version:
            return False
        state.previous, state.current = state.current, version
        state.pending, state.attempts = None, 0
        if state.floor is None or Version.parse(state.floor) < Version.parse(version):
            state.floor = version
        state.save(self.state_file)
        return True

    def restart_needed(self, running: str = __version__) -> bool:
        """A version other than the running one waits to start (installed by `nox node update`)."""
        return self.state().pending not in (None, running)
