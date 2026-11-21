"""The places no tool may ever reach: the node's own folder and the keyring, credential and browser
profile stores of this user (`secret_locations`), and the identities of what lies in them, which
the verdict on an open handle compares against (hard links)."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import time
from pathlib import Path

from nox.node.device.safe_fs.core import _WINDOWS, _plain

#: How many entries of one protected folder are looked at for hard links (bounded per call).
_MAX_PROTECTED_ENTRIES = 512


def secret_locations() -> list[Path]:
    """Where this user's secrets live - credential stores, keys, tokens, browser profiles. No
    file tool ever reads or writes there (a missing one still counts)."""
    home = Path.home()
    found = [
        home / ".ssh",
        home / ".gnupg",
        home / ".aws",
        home / ".azure",
        home / ".kube",
        home / ".docker" / "config.json",
        home / ".git-credentials",
        home / ".netrc",
        home / "_netrc",
        home / ".pgpass",
        home / ".config" / "gcloud",
        home / ".config" / "gh",
    ]
    if _WINDOWS:
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        for base in (roaming, local):
            found += [base / "Microsoft" / name for name in ("Credentials", "Protect", "Vault")]
        found += [
            local / "Google" / "Chrome" / "User Data",
            local / "Microsoft" / "Edge" / "User Data",
            local / "BraveSoftware" / "Brave-Browser" / "User Data",
            local / "Chromium" / "User Data",
            roaming / "Mozilla" / "Firefox",
            roaming / "Opera Software",
        ]
        return found
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        return [
            *found,
            home / "Library" / "Keychains",
            home / "Library" / "Cookies",
            support / "Google" / "Chrome",
            support / "Microsoft Edge",
            support / "BraveSoftware",
            support / "Firefox",
        ]
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [
        *found,
        data / "keyrings",
        data / "kwalletd",
        data / "python_keyring",
        home / ".local" / "share" / "keyrings",
        home / ".local" / "share" / "kwalletd",
        config / "google-chrome",
        config / "chromium",
        config / "microsoft-edge",
        config / "BraveSoftware",
        home / ".mozilla",
        home / ".pki",
    ]


#: How long the protected places' identities are reused: walking browser profiles on every call
#: costs a third of a second; a hard link made in this window is still refused by path.
PROTECTED_TTL_S = 30.0


_protected_cache: dict[
    tuple[str, ...], tuple[float, tuple[str, ...], frozenset[tuple[int, int]]]
] = {}


def _protected(places: tuple[str, ...]) -> tuple[tuple[str, ...], frozenset[tuple[int, int]]]:
    """The protected places, resolved, and the identities of what lies in them (cached)."""
    now = time.monotonic()
    cached = _protected_cache.get(places)
    if cached is not None and now - cached[0] < PROTECTED_TTL_S:
        return cached[1], cached[2]
    guarded: list[str] = []
    ids: set[tuple[int, int]] = set()
    for place in places:
        try:
            real = _plain(os.path.realpath(place))
            info = os.stat(real)
        except (OSError, ValueError):
            guarded.append(os.path.abspath(place))  # not there yet: still never inside it
            continue
        guarded.append(real)
        ids.add((info.st_dev, info.st_ino))
        if stat.S_ISDIR(info.st_mode):
            ids |= _identities(Path(real))
    result = (tuple(guarded), frozenset(ids))
    _protected_cache[places] = (now, *result)
    return result


#: Secrets in big stores (browser profiles, key folders) - looked at before the bounded walk.
_HIGH_VALUE = (
    "Local State",
    "*/Login Data",
    "*/Cookies",
    "*/Network/Cookies",
    "*/Web Data",
    "*/key4.db",
    "*/logins.json",
    "*/cookies.sqlite",
    "Profiles/*/key4.db",
    "Profiles/*/logins.json",
    "Profiles/*/cookies.sqlite",
    "id_*",
    "*.pem",
    "*.key",
    "credentials",
    "config.json",
)


def _identities(folder: Path) -> set[tuple[int, int]]:
    """(st_dev, st_ino) of what lies in a protected folder (two levels, bounded): a hard link to
    one of those files is refused."""
    found: set[tuple[int, int]] = set()
    # The files that matter most first, wherever the generic walk would run out of budget.
    for pattern in _HIGH_VALUE:
        with contextlib.suppress(OSError, ValueError):
            for match in list(folder.glob(pattern))[:64]:
                with contextlib.suppress(OSError):
                    info = os.stat(match, follow_symlinks=False)
                    found.add((info.st_dev, info.st_ino))
    budget = _MAX_PROTECTED_ENTRIES
    with contextlib.suppress(OSError):
        for base, dirs, files in os.walk(folder, followlinks=False):
            if Path(base) != folder and Path(base).parent != folder:
                dirs[:] = []  # two levels are enough for keys and tokens
            for name in [*dirs, *files]:
                budget -= 1
                if budget < 0:
                    return found
                with contextlib.suppress(OSError):
                    info = os.stat(os.path.join(base, name), follow_symlinks=False)
                    found.add((info.st_dev, info.st_ino))
    return found
