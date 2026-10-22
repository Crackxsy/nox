"""Opening a path for a node's `file.*` tools: inside a folder this device allows, decided on the
real file at the moment it is opened - never on the text the core sent.

Order for every open (list, read, write):

1. **The text** (`split`): UNC and Win32 device paths, `..`, a leading `~`, 8.3 short names
   (`NOXNOD~1`), NUL, alternate data streams (`a.txt:s`), Windows device names (`CON`, `aux.txt`)
   and names Windows would silently change (a trailing dot or space) are refused before anything
   touches the file system - an SMB path is never resolved, so the device never reaches out. A
   relative path is relative to the first allowed folder; an absolute one must spell one of them.
   A write is also refused when the path is an **execution trigger** (`execution_trigger`): a
   `.git` folder (hooks), a startup or autostart folder, a shell start file, a script or program.
2. **The open, bound to a handle**:
   * POSIX - `openat`-style walk from the allowed folder (whose identity was recorded when the
     folders were built): each entry is looked at first without following it (`fstatat`
     `AT_SYMLINK_NOFOLLOW` - a link, FIFO, socket or device is refused before it is opened), then
     opened `O_NOFOLLOW | O_NOCTTY | O_NONBLOCK` relative to the directory before it, and every
     directory passed is compared with the protected folders by `(st_dev, st_ino)`.
   * Windows - `CreateFileW` without `FILE_SHARE_DELETE` (the file cannot be renamed away while it
     is open), then `GetFinalPathNameByHandleW` on that handle: the path the open really reached
     after junctions, symlinks and 8.3 names.
   A new file is created exclusively (`O_EXCL` / `CREATE_NEW`, never executable) and removed again
   when its final path is not allowed (Windows: through its own handle; POSIX: by name in the
   directory that was checked); an existing file is truncated only after the check.
3. **The verdict on the handle** (`_verify`): inside an allowed folder, not inside a protected
   one (the node's own folder; the keyring, credential and browser-profile stores of this user -
   `secret_locations`), not a hard link to a protected file, a regular file or a directory. For a
   write also: no second hard link, no executable bit, no execution trigger on the final path.

Everything read or written afterwards goes through that one handle, so swapping the path between
the check and the use changes nothing (`test_c5.py` swaps it). What the OS cannot bind to a handle
is said plainly: listing a folder on Windows enumerates by its verified path while the folder's
handle is held open and compares the folder's identity before and after.
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import functools
import json
import os
import re
import stat
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nox.node.gate import is_remote_or_device_path, names_a_path_alias

__all__ = [
    "Folders",
    "OutsideFolders",
    "broad_folder",
    "execution_trigger",
    "list_folder",
    "open_existing",
    "open_new",
    "content_refusal",
    "secret_locations",
    "structured",
    "write_refusal",
]

_WINDOWS = sys.platform == "win32"
#: Win32 device names, with or without an extension (`nul.txt` is still NUL on most Windows).
_DEVICE_NAME = re.compile(
    r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.I
)
#: How many entries of one protected folder are looked at for hard links (bounded per call).
_MAX_PROTECTED_ENTRIES = 512
#: A FIFO opened for reading would block; with `O_NONBLOCK` it fails or returns at once.
_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)
#: A terminal device never becomes the node's controlling terminal.
_NOCTTY: int = getattr(os, "O_NOCTTY", 0)

#: A write here would make something run outside the sandbox - by git, the desktop, a login shell
#: or a double click. Compared case-insensitively on the path's parts.
_TRIGGER_DIRS: tuple[tuple[str, ...], ...] = (
    (".git",),
    ("start menu", "programs", "startup"),
    (".config", "autostart"),
    (".config", "systemd"),
    (".config", "fish"),
    (".config", "environment.d"),
    ("library", "launchagents"),
    ("library", "launchdaemons"),
    ("windowspowershell",),
    ("powershell",),
)
_TRIGGER_FILES = frozenset(
    {
        ".bashrc",
        ".bash_profile",
        ".bash_login",
        ".bash_logout",
        ".profile",
        ".zshrc",
        ".zprofile",
        ".zshenv",
        ".zlogin",
        ".login",
        ".cshrc",
        ".tcshrc",
        ".xprofile",
        ".xinitrc",
        ".xsessionrc",
        ".pam_environment",
        "autorun.inf",
        "desktop.ini",
    }
)
_TRIGGER_EXTENSIONS = frozenset(
    {
        ".lnk",
        ".url",
        ".pif",
        ".scf",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".psd1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".hta",
        ".scr",
        ".exe",
        ".dll",
        ".com",
        ".cpl",
        ".msi",
        ".msp",
        ".reg",
        ".inf",
        ".appref-ms",
        ".jar",
        ".desktop",
        ".service",
        ".timer",
        ".plist",
        ".sh",
        ".bash",
        ".zsh",
        ".command",
        ".app",
    }
)


#: What a node may write at all (case-insensitive) - `file.write` writes UTF-8 text, so only
#: text formats. PASSIVE: text nothing acts on when it is opened - a new file is medium risk.
#: STRUCTURED: JSON and YAML, which a program may read - every write is high risk, asked on the
#: device and never remembered, and the text must parse plainly (`content_refusal`). Anything
#: else is refused - documents, pages, PDFs and media (`format_not_writable`: write .md or .txt
#: instead), scripts, programs, configuration formats, no extension. An allowlist, because a
#: list of dangerous names or contents can never be complete.
PASSIVE_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".tsv", ".log", ".ics", ".vcf", ".srt"})
STRUCTURED_EXTENSIONS = frozenset({".json", ".yaml", ".yml"})
#: Formats an application interprets (a macro, a script, an action) or binary formats a text
#: write cannot produce: never written on a device, with a reason that says what to use.
NOT_WRITABLE_EXTENSIONS = frozenset(
    {
        ".html",
        ".htm",
        ".xhtml",
        ".svg",
        ".xml",
        ".pdf",
        ".odt",
        ".ods",
        ".odp",
        ".doc",
        ".docx",
        ".docm",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".ppt",
        ".pptx",
        ".pptm",
        ".rtf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".mp4",
        ".webm",
        ".mkv",
    }
)
WRITABLE_EXTENSIONS = PASSIVE_EXTENSIONS | STRUCTURED_EXTENSIONS
#: Files tools read as their configuration: refused by name or pattern even with an allowed
#: extension (an extra layer - the tiers and the working-tree rule are the boundary).
_CONFIG_PATTERNS = (
    "settings.json",
    "tasks.json",
    "launch.json",
    "launchsettings.json",
    "keybindings.json",
    "package.json",
    "package-lock.json",
    "project.json",
    "tsconfig.json",
    "jsconfig.json",
    "composer.json",
    "deno.json",
    "flows.json",
    "appsettings*.json",
    "pom.xml",
    "build.xml",
    "*compose*.yml",
    "*compose*.yaml",
    "lefthook*.yml",
    "lefthook*.yaml",
    "*pipelines*.yml",
    "*pipelines*.yaml",
    "taskfile.yml",
    "taskfile.yaml",
    "configuration.yaml",
    "automations.yaml",
)
#: A folder holding one of these is a working tree: nothing at or below it is written.
WORKING_TREE_MARKERS = (".git", ".hg", ".svn")


def extension_of(name: str) -> str:
    folded = name.casefold()
    return folded[folded.rindex(".") :] if "." in folded else ""


def write_refusal(parts: list[str]) -> str | None:
    """Why a write to the path `parts` (below the allowed folder) is refused, or None: no hidden
    file or folder anywhere (`.git`, `.vscode`, `.bashrc`, ...), only `WRITABLE_EXTENSIONS`, no
    well-known configuration file, nothing in a `workflows` folder."""
    names = [p for p in parts if p]
    if not names:
        return "extension_refused"
    if any(p.startswith(".") for p in names):
        return "hidden_refused"
    folded = [p.casefold() for p in names]
    name = folded[-1]
    if extension_of(name) in NOT_WRITABLE_EXTENSIONS:
        return "format_not_writable"
    if extension_of(name) not in WRITABLE_EXTENSIONS:
        return "extension_refused"
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _CONFIG_PATTERNS):
        return "config_refused"
    if "workflows" in folded[:-1]:
        return "config_refused"
    return None


def structured(path: str) -> bool:
    """Whether a write to `path` is in the STRUCTURED tier (always high risk)."""
    return extension_of(re.split(r"[\\/]", path)[-1]) in STRUCTURED_EXTENSIONS


#: YAML that a loader could turn into more than data: tags (`!!python/...`, any `!tag`), and
#: anchors/aliases (the "billion laughs" expansion) - refused outright.
_YAML_ACTIVE = ("TagToken", "AnchorToken", "AliasToken")


def content_refusal(path: str, text: str) -> str | None:
    """Why the text of a STRUCTURED write is refused even when confirmed: JSON that does not
    parse; YAML that does not parse plainly (`safe_load`) or carries a tag, an anchor or an
    alias. Plain text formats are not looked at."""
    ext = extension_of(re.split(r"[\\/]", path)[-1])
    if ext == ".json":
        try:
            json.loads(text)
        except ValueError:
            return "invalid_content"
        return None
    if ext in (".yaml", ".yml"):
        import yaml  # noqa: PLC0415

        try:
            for token in yaml.scan(text, Loader=yaml.SafeLoader):
                if type(token).__name__ in _YAML_ACTIVE:
                    return "content_refused"
            yaml.safe_load(text)
        except yaml.YAMLError:
            return "invalid_content"
        return None
    return None


class OutsideFolders(Exception):  # noqa: N818 - a refusal, named for what it says
    """The path is not one this device lets Nox open. `reason` is a short machine token."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


def _plain(path: str) -> str:
    """A resolved Windows path without the long-path prefix, the way the answers of
    `GetFinalPathNameByHandleW` are compared (`\\\\?\\C:\\x` -> `C:\\x`)."""
    return path[4:] if _WINDOWS and path.startswith("\\\\?\\") and path[5:6] == ":" else path


def _norm(path: str) -> str:
    text = path.replace("/", "\\") if _WINDOWS else path
    return os.path.normcase(text.rstrip("\\/")) or text


def _within(path: str, folder: str) -> bool:
    inner, outer = _norm(path), _norm(folder)
    sep = "\\" if _WINDOWS else "/"
    return inner == outer or inner.startswith(outer.rstrip(sep) + sep)


def execution_trigger(path: str) -> bool:
    """Whether writing `path` could make something run outside the sandbox (see the module
    docstring). Case-insensitive, on the path's parts - also for a path that does not exist."""
    parts = [p.casefold() for p in re.split(r"[\\/]+", path) if p]
    if not parts:
        return False
    for pattern in _TRIGGER_DIRS:
        width = len(pattern)
        if any(tuple(parts[i : i + width]) == pattern for i in range(len(parts) - width + 1)):
            return True
    name = parts[-1]
    if name in _TRIGGER_FILES:
        return True
    return any(name.endswith(ext) for ext in _TRIGGER_EXTENSIONS)


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


def broad_folder(path: str) -> bool:
    """Whether `path` (resolved) is too broad to hand to Nox's file tools: a drive root or `/`,
    the home folder or anything above it, a system folder (Windows, Program Files, `/etc`,
    `/usr`, ...), or an application-data root (AppData, `~/Library`, `~/.config`)."""
    real = _plain(os.path.realpath(path))
    if Path(real).parent == Path(real):
        return True
    home = os.path.realpath(Path.home())
    if _within(home, real):
        return True  # the home folder itself, or a folder that contains it (`/home`, Users)
    inside: list[str] = []
    exactly: list[str] = []
    if _WINDOWS:
        for variable in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            if os.environ.get(variable):
                inside.append(os.environ[variable])
        for variable in ("ProgramData", "APPDATA", "LOCALAPPDATA"):
            if os.environ.get(variable):
                exactly.append(os.environ[variable])
        exactly.append(str(Path(home) / "AppData"))
    else:
        inside += ["/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/proc", "/sys"]
        inside += ["/dev", "/run", "/System"]
        exactly += ["/var", "/opt", "/srv", "/Library", "/Applications", "/private"]
        exactly += [str(Path(home) / ".config"), str(Path(home) / ".local")]
        exactly += [str(Path(home) / ".local" / "share"), str(Path(home) / "Library")]
    if any(_within(real, os.path.realpath(p)) for p in inside):
        return True
    return any(_within(os.path.realpath(p), real) for p in exactly)


@dataclass(frozen=True)
class _Root:
    path: str
    #: (st_dev, st_ino) when the folder was built: a folder swapped for another is refused.
    identity: tuple[int, int]


@dataclass(frozen=True)
class Folders:
    """The folders this device allows (`node.yaml: folders`, confirmed on the device) and the
    places no tool may ever reach. Built per call from the confirmed manifest."""

    roots: tuple[_Root, ...]
    protected: tuple[str, ...]
    protected_ids: frozenset[tuple[int, int]]

    @classmethod
    def build(cls, roots: Iterable[str], protected: Iterable[Path]) -> Folders:
        guarded, ids = _protected(tuple(str(place) for place in protected))
        allowed: list[_Root] = []
        for entry in roots:
            text = str(entry).strip()
            if not text or is_remote_or_device_path(text) or not os.path.isabs(text):
                continue
            try:
                real = _plain(os.path.realpath(text))
                info = os.stat(real)
            except (OSError, ValueError):
                continue  # an unplugged drive is not an allowed folder right now
            if not stat.S_ISDIR(info.st_mode) or any(_within(real, g) for g in guarded):
                continue  # a folder inside a protected place never counts
            if broad_folder(real):
                continue  # never a whole drive, the home folder or a system folder
            allowed.append(_Root(real, (info.st_dev, info.st_ino)))
        return cls(tuple(allowed), guarded, ids)

    # -- 1. the text ---------------------------------------------------------------------------

    def split(self, text: str) -> tuple[_Root, list[str]]:
        """The allowed folder a path lies in and its parts below it, from the text alone."""
        if not self.roots:
            raise OutsideFolders("no_folders", "no folder is allowed for Nox on this device")
        raw = text
        if not raw.strip() or "\x00" in raw:
            raise OutsideFolders("invalid_path", "no usable path was given")
        if raw != raw.strip():
            # Never cut silently: the person saw the path with its spaces.
            raise OutsideFolders(
                "invalid_path", "a path with leading or trailing spaces is refused"
            )
        if is_remote_or_device_path(raw) or names_a_path_alias(raw):
            raise OutsideFolders(
                "path_alias_refused", "network, device and alias paths are refused"
            )
        if os.path.isabs(raw) or (_WINDOWS and re.match(r"^[A-Za-z]:", raw)):
            if _WINDOWS and not re.match(r"^[A-Za-z]:[\\/]", raw):
                raise OutsideFolders("invalid_path", "a drive-relative path is refused")
            for root in self.roots:
                if _within(raw, root.path):
                    rest = raw[len(root.path.rstrip("\\/")) :]
                    return root, _parts(rest)
            raise OutsideFolders("outside_folders", "outside the folders allowed on this device")
        return self.roots[0], _parts(raw)

    # -- 3. the verdict on a handle --------------------------------------------------------------

    def _verify(
        self, root: _Root, info: os.stat_result, final: str | None, *, write: bool = False
    ) -> None:
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise OutsideFolders("not_a_file", "only regular files and folders are opened")
        if (info.st_dev, info.st_ino) in self.protected_ids:
            raise OutsideFolders("node_home_protected", "that file is protected on this device")
        if final is not None:
            if any(_within(final, g) for g in self.protected):
                raise OutsideFolders("node_home_protected", "that file is protected on this device")
            if not _within(final, root.path):
                raise OutsideFolders(
                    "outside_folders", "the path leads outside the allowed folders"
                )
        if not write or not stat.S_ISREG(info.st_mode):
            return
        if info.st_nlink > 1:
            raise OutsideFolders("hard_link_refused", "a file with a second name is not written")
        if not _WINDOWS and info.st_mode & 0o111:
            raise OutsideFolders("executable_refused", "an executable file is not written")
        if final is not None:
            refusal = write_refusal(re.split(r"[\\/]+", final[len(root.path.rstrip("\\/")) :]))
            if refusal is not None:
                raise OutsideFolders(refusal, "only data files are written (allowlist)")
            if execution_trigger(final):
                raise OutsideFolders("execution_trigger", "writing there could make something run")
            if _working_tree_up(final, root.path):
                raise OutsideFolders("working_tree_refused", "nothing in a repository is written")


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


def _parts(rest: str) -> list[str]:
    parts = [p for p in re.split(r"[\\/]+", rest) if p not in ("", ".")]
    for part in parts:
        if part == "..":
            raise OutsideFolders("path_alias_refused", "`..` is refused")
        if part != part.strip():
            raise OutsideFolders(
                "invalid_path", "a name with leading or trailing spaces is refused"
            )
        if _WINDOWS and (":" in part or part[-1] in ". " or _DEVICE_NAME.match(part)):
            raise OutsideFolders("invalid_path", "a stream, device or ambiguous name is refused")
    return parts


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


# -- 2. the open --------------------------------------------------------------------------------


def open_existing(folders: Folders, text: str, *, write: bool = False) -> int:
    """An fd for an existing file (or folder, read-only), checked on the handle."""
    root, parts = folders.split(text)
    if write:
        _lexical_write_check(root, parts)
    if _WINDOWS:
        return _win_open(folders, root, parts, write=write, create=False)
    flags = (os.O_WRONLY if write else os.O_RDONLY) | _NONBLOCK | _NOCTTY
    return _posix_open(folders, root, parts, flags, write=write, create=False)


def open_new(folders: Folders, text: str) -> int:
    """An fd for a file that must not exist yet (never follows a link at its name)."""
    root, parts = folders.split(text)
    if not parts:
        raise OutsideFolders("invalid_path", "a folder cannot be written as a file")
    _lexical_write_check(root, parts)
    if _WINDOWS:
        return _win_open(folders, root, parts, write=True, create=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NONBLOCK | _NOCTTY
    return _posix_open(folders, root, parts, flags, write=True, create=True)


def _lexical_write_check(root: _Root, parts: list[str]) -> None:
    refusal = write_refusal(parts)
    if refusal is not None:
        raise OutsideFolders(refusal, "only data files are written (allowlist)")
    for depth in range(len(parts)):
        if _working_tree(os.path.join(root.path, *parts[:depth])):
            raise OutsideFolders("working_tree_refused", "nothing in a repository is written")
    if execution_trigger(os.path.join(root.path, *parts)):
        raise OutsideFolders("execution_trigger", "writing there could make something run")


@contextlib.contextmanager
def list_folder(folders: Folders, text: str) -> Iterator[Iterator[os.DirEntry[str]]]:
    """The entries of one allowed folder, enumerated through (POSIX) or under (Windows) its
    verified handle. Entries are never followed: a link is reported as one."""
    fd = open_existing(folders, text)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OutsideFolders("not_a_folder", "that is a file, not a folder")
        if not _WINDOWS:
            scan: Any = os.scandir  # by fd: POSIX only (typeshed says so per platform)
            with scan(fd) as entries:
                yield entries
            return
        final = _win_final(fd)
        before = os.fstat(fd).st_ino
        if final is None or os.stat(final).st_ino != before:
            raise OutsideFolders("changed", "the folder changed while it was opened")
        with os.scandir(final) as entries:
            yield entries
        if os.stat(final).st_ino != before:
            raise OutsideFolders("changed", "the folder changed while it was listed")
    finally:
        os.close(fd)


def _working_tree(folder: str) -> bool:
    """Whether `folder` is the top of a working tree (holds `.git`, `.hg` or `.svn`)."""
    return any(os.path.lexists(os.path.join(folder, marker)) for marker in WORKING_TREE_MARKERS)


def _working_tree_up(final: str, root: str) -> bool:
    """Whether any folder from the one holding `final` up to the allowed folder is a working
    tree - decided on the verified final path."""
    folder = os.path.dirname(final)
    while _within(folder, root):
        if _working_tree(folder):
            return True
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return False


def _peek(name: str, dir_fd: int) -> os.stat_result | None:
    """The entry as it is (`fstatat` without following), None when there is none."""
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _posix_open(
    folders: Folders, root: _Root, parts: list[str], flags: int, *, write: bool, create: bool
) -> int:
    """Walk from the allowed folder; look at every entry before opening it and never follow a
    link. A new file is only its own entry (`O_CREAT | O_EXCL`, mode 0600), in a directory that
    was itself checked on the way - and is removed from it again if its check fails."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    fd = os.open(root.path, os.O_RDONLY | directory | cloexec)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != root.identity:
            raise OutsideFolders("changed", "the allowed folder was replaced")
        if write and any(_peek(marker, fd) is not None for marker in WORKING_TREE_MARKERS):
            raise OutsideFolders("working_tree_refused", "nothing in a repository is written")
        for part in parts[:-1]:
            seen = _peek(part, fd)
            if seen is None:
                raise FileNotFoundError(errno.ENOENT, "no such folder", part)
            if stat.S_ISLNK(seen.st_mode):
                raise OutsideFolders("link_refused", "links are not followed on this device")
            if not stat.S_ISDIR(seen.st_mode):
                raise NotADirectoryError(errno.ENOTDIR, "not a folder", part)
            try:
                step = os.open(part, os.O_RDONLY | directory | nofollow | cloexec, dir_fd=fd)
            except OSError as exc:
                raise _link_or(exc) from exc
            os.close(fd)
            fd = step
            folders._verify(root, os.fstat(fd), None)
            if write and any(_peek(m, fd) is not None for m in WORKING_TREE_MARKERS):
                raise OutsideFolders("working_tree_refused", "nothing in a repository is written")
        if not parts:
            final = os.dup(fd)
        else:
            seen = _peek(parts[-1], fd)
            if seen is not None:
                if create:
                    raise FileExistsError(errno.EEXIST, "the file exists", parts[-1])
                if stat.S_ISLNK(seen.st_mode):
                    raise OutsideFolders("link_refused", "links are not followed on this device")
                if not (stat.S_ISREG(seen.st_mode) or stat.S_ISDIR(seen.st_mode)):
                    raise OutsideFolders("not_a_file", "only regular files and folders are opened")
            elif not create:
                raise FileNotFoundError(errno.ENOENT, "no such file", parts[-1])
            try:
                final = os.open(parts[-1], flags | nofollow | cloexec, 0o600, dir_fd=fd)
            except OSError as exc:
                raise _link_or(exc) from exc
        try:
            folders._verify(root, os.fstat(final), _final_path(final), write=write)
        except BaseException:
            os.close(final)
            if create:
                with contextlib.suppress(OSError):
                    os.unlink(parts[-1], dir_fd=fd)
            raise
    finally:
        os.close(fd)
    if flags & _NONBLOCK:
        os.set_blocking(final, True)
    return final


def _link_or(exc: OSError) -> Exception:
    """`O_NOFOLLOW` on a link that appeared after the look answers ELOOP."""
    if exc.errno in (errno.ELOOP, errno.EMLINK):
        return OutsideFolders("link_refused", "links are not followed on this device")
    return exc


def _final_path(fd: int) -> str | None:
    """The kernel's own name for an fd: `/proc/self/fd` on Linux, `F_GETPATH` on macOS."""
    if sys.platform == "darwin":
        import fcntl

        getpath = getattr(fcntl, "F_GETPATH", None)
        if getpath is None:
            return None
        with contextlib.suppress(OSError):
            raw: bytes = fcntl.fcntl(fd, getpath, bytes(1024))
            return os.fsdecode(raw.split(b"\x00", 1)[0])
        return None
    with contextlib.suppress(OSError):
        return os.readlink(f"/proc/self/fd/{fd}")
    return None


# -- Windows --------------------------------------------------------------------------------------

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_DISPOSITION_INFO = 4
_FILE_TYPE_DISK = 1


@functools.cache
def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    loader: Any = ctypes
    k32 = loader.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    k32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    k32.SetFileInformationByHandle.restype = wintypes.BOOL
    k32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.GetFileType.restype = wintypes.DWORD
    k32.GetFileType.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _win_final_handle(k32: Any, handle: int) -> str | None:
    """`GetFinalPathNameByHandleW` (normalized, DOS volume name): the real path of a handle,
    without the `\\\\?\\` prefix. A network path (`\\\\?\\UNC\\...`) is None (refused)."""
    import ctypes

    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = k32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if length == 0:
            return None
        if length < size:
            break
        size = length + 1
    text = buffer.value
    if text.upper().startswith("\\\\?\\UNC\\"):
        return None
    return text[4:] if text.startswith("\\\\?\\") else text


def _msvcrt() -> Any:
    import importlib

    return importlib.import_module("msvcrt")


def _win_final(fd: int) -> str | None:
    return _win_final_handle(_kernel32(), _msvcrt().get_osfhandle(fd))


def _win_open(folders: Folders, root: _Root, parts: list[str], *, write: bool, create: bool) -> int:
    import ctypes

    msvcrt = _msvcrt()
    winapi: Any = ctypes
    k32 = _kernel32()
    path = os.path.join(root.path, *parts)
    access = (_GENERIC_WRITE | _GENERIC_READ) if write else _GENERIC_READ
    if create:
        access |= _DELETE  # to take a wrongly placed new file back through this same handle
    # The long-path form: no further normalisation by Win32 (the parts were checked above).
    handle = k32.CreateFileW(
        path if path.startswith("\\\\?\\") else "\\\\?\\" + path,
        access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,  # never FILE_SHARE_DELETE: no rename while open
        None,
        _CREATE_NEW if create else _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise winapi.WinError(winapi.get_last_error())

    def take_back(raw: int) -> None:
        flag = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile = TRUE
        k32.SetFileInformationByHandle(raw, _FILE_DISPOSITION_INFO, ctypes.byref(flag), 1)

    try:
        if k32.GetFileType(handle) != _FILE_TYPE_DISK:
            raise OutsideFolders("not_a_file", "only files on a disk are opened")
        final = _win_final_handle(k32, handle)
        if final is None:
            raise OutsideFolders("outside_folders", "the path leads to a network or unknown place")
        fd: int = msvcrt.open_osfhandle(handle, os.O_RDWR if write else os.O_RDONLY)
    except BaseException:
        if create:
            take_back(handle)
        k32.CloseHandle(handle)
        raise
    try:
        folders._verify(root, os.fstat(fd), final, write=write)
    except BaseException:
        if create:
            take_back(msvcrt.get_osfhandle(fd))
        os.close(fd)
        raise
    return fd
