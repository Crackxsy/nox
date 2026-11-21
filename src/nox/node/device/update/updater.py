"""`NodeUpdater`: one check from the signed manifest to the pending version, in the order the
package docstring lists, and the trial start of the new copy."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from nox import __version__
from nox.node.device.bundle import ARCHIVE_NAME, BundleError, bundle_name, sha256_of, unpack
from nox.node.device.cli_text import command
from nox.node.device.update.fetch import Fetcher, HttpsFetcher
from nox.node.device.update.state import (
    _CREATED,
    _VERSION_DIR,
    BAD_FOR_DAYS,
    Kind,
    Status,
    UpdateCheck,
    UpdateState,
    _now,
    state_lock,
)
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

#: `nox node run` ends with this when it should start again (a new version is in place).
RESTART_EXIT_CODE = 75


#: ... and with this when its core refused the protocol of a version on trial: roll it back.
TRIAL_FAILED_EXIT_CODE = 76


MAX_BUNDLE_BYTES = 64 * 1024 * 1024


#: No newer signed manifest for this long: say so (a frozen mirror, or simply no releases).
STALE_AFTER_DAYS = 30


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
        "import nox, nox.cli, nox.node.device.agent, nox.node.device.update; print(nox.__version__)"
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


def _stamp(text: str) -> dt.datetime | None:
    """A manifest's `created` (`YYYY-MM-DDTHH:MM:SSZ`, zero-padded, UTC) as a time; None else."""
    if not _CREATED.match(text):
        return None
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


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
        # Freshness: a validly signed but older manifest is a replay (or a frozen mirror); one
        # "created" more than a day ahead of this clock cannot be judged and would block every
        # later one, so it is refused too.
        created = _stamp(found.created)
        if created is None or created > self._clock() + dt.timedelta(days=1):
            return _refused("bad_created", f"the manifest's time {found.created[:40]!r} is wrong")
        newest = _stamp(state.newest_created) if state.newest_created else None
        if newest is not None and created < newest:
            return _refused(
                "stale",
                f"the release information ({found.created}) is older than what this node has "
                f"already seen ({state.newest_created})",
            )
        if newest is None or created > newest:
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
                f"with `{_node_command('update --yes')}`",
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
                f"Nox {name} is available ({'; '.join(reasons)}): run "
                f"`{_node_command('update')}` on this device.",
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


def _node_command(rest: str) -> str:
    """The node's command as this device needs it (`nox.node.device.cli_text.command`)."""

    return command(rest)
