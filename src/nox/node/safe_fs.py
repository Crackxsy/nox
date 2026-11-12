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

`file.move` and `file.delete` (`move_file`, `trash_file`) take the same three steps for both ends of
a move and for the file to delete, then act relative to the verified handles - a rename without
replacing, into the trash only (never an unlink); see their docstrings.

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
import html
import json
import os
import re
import stat
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator
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
    "move_file",
    "open_existing",
    "open_new",
    "INERT_FENCE_LANGUAGES",
    "content_refusal",
    "marked_above",
    "marking",
    "markdown_finding",
    "markdown_runs_code",
    "secret_locations",
    "structured",
    "trash_file",
    "trash_support",
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
#: A folder holding one of these is a working tree: nothing at or below it is written - also
#: when the working tree lies above the allowed folder.
WORKING_TREE_MARKERS = (".git", ".hg", ".svn", ".jj", ".bzr", "_darcs", ".sl")
#: A folder holding one of these is an app workspace that runs what its notes say (an Obsidian
#: vault runs `dataviewjs` blocks and Templater tags, a Logseq graph its config, an editor its
#: tasks): treated like a working tree. Allow a plain folder outside the workspace instead.
APP_WORKSPACE_MARKERS = (
    ".obsidian",
    ".logseq",
    "logseq/config.edn",
    ".foam",
    ".dendron",
    "dendron.yml",
    ".vscode",
    ".idea",
)
#: The largest JSON/YAML a node writes (plain text: `MAX_TEXT_BYTES`): parsing stays well
#: under the check's time limit, also with pure-Python YAML on a Raspberry Pi.
MAX_STRUCTURED_BYTES = 64 * 1024


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


#: Markdown a note app could run as code - refused in any `.md`, workspace or not. These are
#: position-independent tokens, not a Markdown parser: a fence inside a blockquote, a list item
#: or an unbalanced fence is still found. A note that merely mentions one of them is refused too
#: (accepted: Nox cannot write notes that mention these plugin keywords).
_MD_TOKENS = (
    "dataviewjs",
    "dataview-js",
    "js-engine",
    "templater",
    "meta-bind-js",
    "<%",
    "<script",
    "javascript:",
    "vbscript:",
    "obsidian://",
    "<iframe",
    "<object",
    "<embed",
)
#: Code-block languages that are data or plain text: a fence may name only these (or nothing).
#: Any other name could be a plugin's runnable block (Dataview JS, JS Engine, Templater,
#: Datacore, ...), a language the app renders (Mermaid, math, HTML, XML, CSS) or one a plugin
#: runs on a click (Execute Code runs Python, shells, JS, ...) - and no list of those is complete.
INERT_FENCE_LANGUAGES = frozenset(
    {
        "text",
        "plaintext",
        "txt",
        "md",
        "markdown",
        "json",
        "yaml",
        "yml",
        "toml",
        "ini",
        "csv",
        "diff",
        "log",
    }
)
#: The largest text a node writes (and the most of an existing file an append reads): every
#: check stays a few milliseconds, whatever the text.
MAX_TEXT_BYTES = 256 * 1024
#: The scan's patterns are linear without any cap that could turn into a skip: no quantifier
#: can overlap the next one, and nothing has to be given back. A fence run takes the whole run
#: of backticks/tildes greedily, the blanks after it and the whole info word (each may be empty),
#: so a match never fails or backtracks; `next` is the character right after the info word.
_FENCE_INFO = re.compile(r"(?:`{3,}|~{3,})[ \t]*\{?[ \t]*\.?([^\s`~}]*)(.?)", re.S)
#: Inline JS: a backtick or tilde, any whitespace, `$=` or `=this.` (whitespace cannot overlap
#: either side).
_INLINE_JS = re.compile(r"[`~]\s*(?:\$=|=this\.)")
#: An event handler inside a raw HTML tag; the tag's text cannot run past the next `<` or `>`,
#: so each tag is read once.
_HANDLER = re.compile(r"<[a-z][^<>]*?\bon[a-z0-9_]+\s*=")
#: The longest code-block language Nox accepts; anything longer is refused, never cut.
MAX_FENCE_LANGUAGE = 64
_FILE_LINK = re.compile(r"(?<![a-z0-9])file:")
_TEMPLATER_API = re.compile(r"\btp\.")


def markdown_finding(text: str) -> str | None:
    """What in Markdown a note app might run, in a few words - or None. After HTML entities are
    decoded, NFKC applied and case folded, with linear patterns only:

    * every run of three or more backticks or tildes, wherever it stands, names no language or
      one of `INERT_FENCE_LANGUAGES`; a backtick or tilde followed by `$=` or `=this.` is
      inline JS;
    * none of `_MD_TOKENS`, no `file:` link, no `%>` together with Templater's `tp.`;
    * no `on...=` handler inside a raw HTML tag.
    """
    body = unicodedata.normalize("NFKC", html.unescape(text)).casefold()
    for token in _MD_TOKENS:
        if token in body:
            return f"{token!r}"
    if _FILE_LINK.search(body):
        return "a file: link"
    if "%>" in body and _TEMPLATER_API.search(body):
        return "a Templater tag"
    if _INLINE_JS.search(body):
        return "inline Dataview JS"
    for fence in _FENCE_INFO.finditer(body):
        language, after = fence.group(1), fence.group(2)
        if len(language) > MAX_FENCE_LANGUAGE:
            return "an overlong code block language"
        if language and language not in INERT_FENCE_LANGUAGES:
            return f"a {language[:MAX_FENCE_LANGUAGE]!r} code block"
        if not language and after not in ("", "\n", "\r", "`", "~", "}"):
            # Something other than the end of the line follows the fence without a word
            # (a vertical tab, a form feed, ...): refused rather than guessed.
            return "an unreadable code block language"
    if _HANDLER.search(body):
        return "an HTML event handler"
    return None


def markdown_runs_code(text: str) -> bool:
    """Whether Markdown carries anything a note app might run (`markdown_finding`)."""
    return markdown_finding(text) is not None


def content_refusal(path: str, text: str) -> str | None:
    """Why the text of a write is refused even when confirmed: over `MAX_TEXT_BYTES`; JSON that
    does not parse; YAML that does not parse plainly (`safe_load`) or carries a tag, an anchor or
    an alias; Markdown with anything a note app might run. Text only, bounded and linear - it
    runs before anyone is asked (off the event loop), on the core and on the node."""
    if len(text) > MAX_TEXT_BYTES or len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        return "too_large"
    ext = extension_of(re.split(r"[\\/]", path)[-1])
    if ext in STRUCTURED_EXTENSIONS and len(text.encode("utf-8", "replace")) > MAX_STRUCTURED_BYTES:
        return "too_large"
    if ext == ".md":
        return "content_refused" if markdown_runs_code(text) else None
    if ext == ".json":
        try:
            json.loads(text)
        except (ValueError, RecursionError):
            return "invalid_content"
        return None
    if ext in (".yaml", ".yml"):
        import yaml  # noqa: PLC0415

        # The C loader where libyaml is there: the same rules, many times faster.
        loader: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        try:
            for token in yaml.scan(text, Loader=loader):
                if type(token).__name__ in _YAML_ACTIVE:
                    return "content_refused"
            yaml.load(text, Loader=loader)  # noqa: S506 - a safe loader, plain or C
        except (yaml.YAMLError, RecursionError):
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
            self._verify_name(root, final)

    def _verify_name(self, root: _Root, final: str) -> None:
        """The write rules on a real, final path (one that exists or one a file is about to get):
        inside the allowed folder and no protected place, an allowlisted data file, no execution
        trigger, no working tree or app workspace around it."""
        if any(_within(final, g) for g in self.protected):
            raise OutsideFolders("node_home_protected", "that file is protected on this device")
        if not _within(final, root.path):
            raise OutsideFolders("outside_folders", "the path leads outside the allowed folders")
        refusal = write_refusal(re.split(r"[\\/]+", final[len(root.path.rstrip("\\/")) :]))
        if refusal is not None:
            raise OutsideFolders(refusal, "only data files are written (allowlist)")
        if execution_trigger(final):
            raise OutsideFolders("execution_trigger", "writing there could make something run")
        marked = marked_above(os.path.dirname(final))
        if marked is not None:
            raise OutsideFolders(marked, "nothing in a repository or app workspace is written")


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
    # Read and write: an append reads what is there through the same handle (`hands._write`).
    flags = (os.O_RDWR if write else os.O_RDONLY) | _NONBLOCK | _NOCTTY
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
    marked = marked_above(os.path.join(root.path, *parts[:-1]))
    if marked is not None:
        raise OutsideFolders(marked, "nothing in a repository or app workspace is written")
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


#: What a vault's config folder holds, whatever it is called (Obsidian lets a vault rename its
#: `.obsidian`): a dot-folder with one of these directly inside marks an app workspace.
VAULT_SIGNATURE = (
    "app.json",
    "appearance.json",
    "community-plugins.json",
    "core-plugins.json",
    "workspace.json",
    "plugins",
    "config.edn",
)
#: Markers that mean a note vault even in the home folder (where `.vscode`, `.idea` or a bare
#: `plugins` folder are an app's own settings, not a workspace).
SPECIFIC_WORKSPACE_MARKERS = (".obsidian", ".logseq", "logseq/config.edn")
#: More entries than any real folder holds: a folder this full is refused, never half-read.
_MAX_FOLDER_ENTRIES = 200_000


def _signature(has: Callable[[str], bool], *, strict: bool) -> bool:
    """Whether a config folder (`has(name)`: it holds `name`) is a vault's. `strict`: only the
    unmistakable combination - `app.json` with `workspace.json` or `core-plugins.json`."""
    if strict:
        return has("app.json") and (has("workspace.json") or has("core-plugins.json"))
    return any(has(name) for name in VAULT_SIGNATURE)


def _holds(folder: str, name: str) -> bool:
    return os.path.lexists(os.path.join(folder, name))


def _holds_at(fd: int, folder: str, name: str) -> bool:
    return _peek(f"{folder}/{name}", fd) is not None


def _signed(folder: str, *, strict: bool = False) -> bool:
    """Whether a dot-folder directly inside `folder` carries a vault's signature. Every entry is
    looked at (listing is cheap; only dot-folders are opened) - a cap would be a skip."""
    try:
        with os.scandir(folder) as entries:
            for count, entry in enumerate(entries):
                if count >= _MAX_FOLDER_ENTRIES:
                    return True  # too full to be sure: refuse
                if not entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                    continue
                if _signature(functools.partial(_holds, entry.path), strict=strict):
                    return True
    except OSError:
        return False
    return False


def marking(folder: str, *, generic: bool = True) -> str | None:
    """`working_tree_refused` when `folder` is the top of a working tree, `app_workspace_refused`
    when it is an app workspace (a marker, or a dot-folder with a vault's signature), else None -
    by name, not following anything. `generic=False` (the home folder and above) looks only for
    unmistakable vaults: `.vscode`, `.idea` or a bare `plugins` folder there are apps' settings."""
    if any(os.path.lexists(os.path.join(folder, m)) for m in WORKING_TREE_MARKERS):
        return "working_tree_refused"
    markers = APP_WORKSPACE_MARKERS if generic else SPECIFIC_WORKSPACE_MARKERS
    if any(os.path.lexists(os.path.join(folder, m)) for m in markers):
        return "app_workspace_refused"
    if _signed(folder, strict=not generic):
        return "app_workspace_refused"
    return None


def marked_above(folder: str) -> str | None:
    """The first marking of `folder` or any folder above it, up to the root of its drive - so a
    repository or a vault around the allowed folder counts as well as one inside it. At the home
    folder and above, generic markers (`.vscode`, `.idea`) do not count, a vault still does."""
    home = _plain(os.path.realpath(Path.home()))
    current = folder
    for _ in range(64):
        at_or_above_home = _within(home, current)
        found = marking(current, generic=not at_or_above_home)
        if found is not None:
            return found
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return "working_tree_refused"  # deeper than any real path: refuse rather than guess


def _marked_fd(fd: int) -> None:
    """The working-tree and workspace rule on a walked directory handle (POSIX) - every entry is
    looked at, only dot-folders are inspected."""
    if any(_peek(m, fd) is not None for m in WORKING_TREE_MARKERS):
        raise OutsideFolders("working_tree_refused", "nothing in a repository is written")
    if any(_peek(m, fd) is not None for m in APP_WORKSPACE_MARKERS):
        raise OutsideFolders("app_workspace_refused", "nothing in an app workspace is written")
    listing: Any = os.listdir  # by fd: POSIX only
    try:
        names = listing(fd)
    except OSError:
        return
    if len(names) > _MAX_FOLDER_ENTRIES:
        raise OutsideFolders("app_workspace_refused", "a folder too full to check is not written")
    for name in names:
        if not name.startswith("."):
            continue
        seen = _peek(name, fd)
        if seen is None or not stat.S_ISDIR(seen.st_mode):
            continue
        if _signature(functools.partial(_holds_at, fd, name), strict=False):
            raise OutsideFolders("app_workspace_refused", "nothing in an app workspace is written")


def _peek(name: str, dir_fd: int) -> os.stat_result | None:
    """The entry as it is (`fstatat` without following), None when there is none."""
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _posix_open(
    folders: Folders, root: _Root, parts: list[str], flags: int, *, write: bool, create: bool
) -> int:
    """Walk from the allowed folder; look at every entry before opening it and never follow a
    link. A new file is only its own entry (`O_CREAT | O_EXCL`, mode 0600), in a directory that
    was itself checked on the way - and is removed from it again if its check fails."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    fd = _posix_walk(folders, root, parts[:-1], write=write)
    try:
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


def _posix_walk(folders: Folders, root: _Root, folder_parts: list[str], *, write: bool) -> int:
    """A directory fd for `root/folder_parts`, walked from the allowed folder (whose identity
    must not have changed): every entry is looked at without following it, opened `O_NOFOLLOW`
    relative to the one before, compared with the protected folders and - for a write - checked
    for a working tree or an app workspace."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    fd = os.open(root.path, os.O_RDONLY | directory | cloexec)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != root.identity:
            raise OutsideFolders("changed", "the allowed folder was replaced")
        if write:
            _marked_fd(fd)
        for part in folder_parts:
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
            if write:
                _marked_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


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


# -- 4. moving and deleting (`file.move`, `file.delete`) -----------------------------------------

#: The largest file a node puts in the trash: far above any note or log, far below what a Recycle
#: Bin holds - a file too large for the bin would be deleted for good by Windows, never that.
MAX_TRASH_BYTES = 256 * 1024 * 1024
_FILE_SHARE_DELETE = 0x4
_FILE_READ_ATTRIBUTES = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_RENAME_INFO = 3
_FILE_STANDARD_INFO = 1
_DRIVE_FIXED = 3
#: `renameat2` (Linux) and `renameatx_np` (macOS): fail instead of replacing the target.
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x4


@dataclass(frozen=True)
class Moved:
    """Where a moved file was and where it is now (the real paths)."""

    source: str
    target: str


def extension_class(name: str) -> str:
    """The kind of checks a file's text gets when written under this name: `md` (nothing a note
    app runs), `json`, `yaml` (parsed plainly, no tags or aliases) or `passive` (plain data, no
    checks beyond the size)."""
    ext = extension_of(name)
    if ext == ".md":
        return "md"
    if ext == ".json":
        return "json"
    if ext in (".yaml", ".yml"):
        return "yaml"
    return "passive"


def _class_refusal(source: str, target: str) -> None:
    """A move keeps the file's class: its text passed the checks of one class, and renamed into
    another it would skip those of the new one (`a.txt` with a Dataview marker becoming `a.md`)."""
    if extension_class(source) != extension_class(target):
        raise OutsideFolders(
            "extension_class_changed",
            "a file keeps its kind: text, Markdown, JSON and YAML are not renamed into each other",
        )


def _target_content_refusal(read: Callable[[int], bytes], size: int, target: str) -> None:
    """The write checks of the TARGET name on the file's own text, read through the verified
    handle - before anything moves (a second line of defence behind `_class_refusal`)."""
    if extension_class(target) == "passive":
        return
    if size > MAX_TEXT_BYTES:
        raise OutsideFolders("too_large", "a file this large is not renamed into a checked kind")
    text = read(MAX_TEXT_BYTES + 1).decode("utf-8", errors="replace")
    refusal = content_refusal(target, text)
    if refusal is not None:
        raise OutsideFolders(refusal, "the text would not be written under that name")


def move_file(folders: Folders, source: str, target: str) -> Moved:
    """Move or rename one regular file inside the allowed folders.

    Both ends follow the write rules (allowlisted data formats only - `notes.txt` never becomes
    `run.bat` -, no hidden names, no working tree or app workspace, no execution trigger, never a
    protected place). A target that is an existing folder gets the file's own name; otherwise the
    target must not exist (a move never replaces anything) and its folder must (folders are never
    created). Links are refused, never followed, at both ends; folders are not moved; a move never
    crosses drives (no copy-and-delete).

    Bound to handles: on POSIX both folders are walked and verified as directory fds and the rename
    is relative to them without replacing (`renameat2(RENAME_NOREPLACE)`,
    `renameatx_np(RENAME_EXCL)`, else `linkat` + `unlinkat`); on Windows the verified source handle
    (held without `FILE_SHARE_DELETE`, so nobody else renames it meanwhile) is renamed into the
    verified target folder, which stays open meanwhile. The moved file is checked again
    afterwards: anything else in its place is moved back and the call refused (`changed`).

    **A file keeps its kind.** Plain text (`.txt`, `.csv`, ...), Markdown, JSON and YAML get
    different content checks when written, so a move refuses any change from one of these kinds
    to another (`extension_class_changed`, both directions: `a.txt` -> `a.md` and `a.md` ->
    `a.txt`); renaming within a kind is fine. And the file's text - read through the verified
    handle, bounded by `MAX_TEXT_BYTES` - must pass the target name's checks too."""
    sroot, sparts = folders.split(source)
    troot, tparts = folders.split(target)
    if not sparts or not tparts:
        raise OutsideFolders("invalid_path", "a folder is not moved; name a file")
    if _is_folder(folders, target):
        tparts = [*tparts, sparts[-1]]
    _class_refusal(sparts[-1], tparts[-1])
    _lexical_write_check(sroot, sparts)
    _lexical_write_check(troot, tparts)
    if _WINDOWS:
        return _win_move(folders, sroot, sparts, troot, tparts)
    return _posix_move(folders, sroot, sparts, troot, tparts)


def trash_support() -> str | None:
    """Why `file.delete` cannot work on this device, or None (looked at without deleting)."""
    if sys.platform == "darwin":
        import importlib.util  # noqa: PLC0415

        if importlib.util.find_spec("Foundation") is None:
            return "the Trash needs PyObjC on a Mac (extra `shell`)"
    return None


def trash_file(folders: Folders, text: str) -> str:
    """Move one regular file into this user's trash - the Recycle Bin on Windows, the Trash on a
    Mac, the freedesktop trash (`~/.local/share/Trash`) on Linux - and return where it went. Never
    an unlink: where the trash cannot take the file (another drive than the user's trash on
    Linux, a removable drive or "delete immediately" on Windows, a file over `MAX_TRASH_BYTES`),
    it is refused (`trash_unavailable`) and the file stays.

    The same rules as a write (allowlisted data formats, no working tree or app workspace, no
    execution trigger, no protected place, no link, no second hard link). On POSIX the file is
    renamed relative to its verified directory fd into the trash (Linux: with its `.trashinfo`
    written first, as the specification says), on a Mac by the system's `trashItemAtURL`; on
    Windows the parent folder is pinned (held open without `FILE_SHARE_DELETE`) while the shell
    recycles the file, and the file's own handle - open the whole time - must afterwards be in
    the Recycle Bin. In every case the trashed item must be the file that was checked
    (`changed` otherwise)."""
    root, parts = folders.split(text)
    if not parts:
        raise OutsideFolders("invalid_path", "a folder is not deleted; name a file")
    _lexical_write_check(root, parts)
    if _WINDOWS:
        return _win_trash(folders, root, parts)
    directory = _posix_walk(folders, root, parts[:-1], write=True)
    try:
        identity, size = _posix_leaf(folders, root, directory, parts[-1])
        if size > MAX_TRASH_BYTES:
            raise OutsideFolders("too_large", "a file this large is not put in the trash here")
        folder = _final_path(directory)
        if folder is None:
            raise OutsideFolders("trash_unavailable", "where the file lies cannot be told here")
        original = os.path.join(folder, parts[-1])
        if sys.platform == "darwin":
            return _mac_trash(original, identity)
        return _xdg_trash(directory, parts[-1], identity, original)
    finally:
        os.close(directory)


def _is_folder(folders: Folders, text: str) -> bool:
    """Whether `text` names an existing folder (checked on its handle, never followed)."""
    try:
        fd = open_existing(folders, text)
    except (OSError, OutsideFolders):
        return False
    try:
        return stat.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def _posix_leaf(
    folders: Folders, root: _Root, directory: int, name: str, *, read_as: str | None = None
) -> tuple[tuple[int, int], int]:
    """The file `name` in a verified directory: looked at without following, opened `O_NOFOLLOW`
    and checked on its handle with the write rules. Its identity and size. With `read_as`, its
    text must also pass the checks of that (target) name."""
    seen = _peek(name, directory)
    if seen is None:
        raise FileNotFoundError(errno.ENOENT, "no such file", name)
    if stat.S_ISLNK(seen.st_mode):
        raise OutsideFolders("link_refused", "links are not followed on this device")
    if stat.S_ISDIR(seen.st_mode):
        raise OutsideFolders("not_a_file", "folders are not moved or deleted, only files")
    if not stat.S_ISREG(seen.st_mode):
        raise OutsideFolders("not_a_file", "only regular files are moved or deleted")
    flags = os.O_RDONLY | _NONBLOCK | _NOCTTY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags | getattr(os, "O_CLOEXEC", 0), dir_fd=directory)
    except OSError as exc:
        raise _link_or(exc) from exc
    try:
        info = os.fstat(fd)
        folders._verify(root, info, _final_path(fd), write=True)
        if read_as is not None:
            _target_content_refusal(lambda n: os.read(fd, n), info.st_size, read_as)
    finally:
        os.close(fd)
    identity = (info.st_dev, info.st_ino)
    if identity != (seen.st_dev, seen.st_ino):
        raise OutsideFolders("changed", "the file changed while it was opened")
    return identity, info.st_size


def _posix_move(
    folders: Folders, sroot: _Root, sparts: list[str], troot: _Root, tparts: list[str]
) -> Moved:
    source_dir = _posix_walk(folders, sroot, sparts[:-1], write=True)
    try:
        target_dir = _posix_walk(folders, troot, tparts[:-1], write=True)
        try:
            identity, _size = _posix_leaf(
                folders, sroot, source_dir, sparts[-1], read_as=tparts[-1]
            )
            if _peek(tparts[-1], target_dir) is not None:
                raise FileExistsError(errno.EEXIST, "the target exists", tparts[-1])
            if os.fstat(target_dir).st_dev != identity[0]:
                raise OutsideFolders("cross_device", "a file is moved only within one drive")
            source_folder, target_folder = _final_path(source_dir), _final_path(target_dir)
            if source_folder is None or target_folder is None:
                raise OutsideFolders("changed", "where the folders lie cannot be told here")
            target_final = os.path.join(target_folder, tparts[-1])
            folders._verify_name(troot, target_final)
            _rename_noreplace(source_dir, sparts[-1], target_dir, tparts[-1])
            moved = os.stat(tparts[-1], dir_fd=target_dir, follow_symlinks=False)
            if (moved.st_dev, moved.st_ino) != identity:
                with contextlib.suppress(OSError):
                    _rename_noreplace(target_dir, tparts[-1], source_dir, sparts[-1])
                raise OutsideFolders("changed", "the file changed while it was moved (moved back)")
            return Moved(os.path.join(source_folder, sparts[-1]), target_final)
        finally:
            os.close(target_dir)
    finally:
        os.close(source_dir)


@functools.cache
def _libc() -> Any:
    import ctypes  # noqa: PLC0415

    loader: Any = ctypes
    if sys.platform == "darwin":
        return loader.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    return loader.CDLL(None, use_errno=True)


def _rename_noreplace(source_dir: int, source: str, target_dir: int, target: str) -> None:
    """Rename `source` (in `source_dir`) to `target` (in `target_dir`), failing with
    `FileExistsError` when the target exists - atomically where the system can (`renameat2`
    with `RENAME_NOREPLACE`, `renameatx_np` with `RENAME_EXCL`), else as a hard link (which
    fails on an existing name) followed by removing the old name."""
    import ctypes  # noqa: PLC0415

    function: Any = None
    flag = _RENAME_EXCL if sys.platform == "darwin" else _RENAME_NOREPLACE
    with contextlib.suppress(OSError, AttributeError):
        function = getattr(_libc(), "renameatx_np" if sys.platform == "darwin" else "renameat2")
    if function is not None:
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p]
        function.argtypes += [ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(source_dir, os.fsencode(source), target_dir, os.fsencode(target), flag) == 0:
            return
        code = ctypes.get_errno()
        unsupported = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", 0)}
        if code not in unsupported:
            raise OSError(code, os.strerror(code), target)
    os.link(source, target, src_dir_fd=source_dir, dst_dir_fd=target_dir, follow_symlinks=False)
    try:
        os.unlink(source, dir_fd=source_dir)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(target, dir_fd=target_dir)
        raise


def _trash_names(name: str) -> Iterator[str]:
    """`name`, then `name.2`, `name.3`, ... (before the extension) - a free name in the trash."""
    yield name
    stem, dot, ext = name.rpartition(".")
    if not stem:
        stem, dot, ext = name, "", ""
    for number in range(2, 1000):
        yield f"{stem}.{number}{dot}{ext}"


def _xdg_trash(directory: int, name: str, identity: tuple[int, int], original: str) -> str:
    """The freedesktop trash in this user's data folder: `info/<name>.trashinfo` is created
    exclusively first (it reserves the name), then the file is renamed into `files/` relative to
    both verified folders. Only on the drive the trash is on - a file on another drive is
    refused, never copied."""
    from urllib.parse import quote  # noqa: PLC0415

    data = os.environ.get("XDG_DATA_HOME") or ""
    base = Path(data) if os.path.isabs(data) else Path.home() / ".local" / "share"
    trash = base / "Trash"
    nofollow, cloexec = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_CLOEXEC", 0)
    folder_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    try:
        trash.mkdir(mode=0o700, parents=True, exist_ok=True)
        trash_fd = os.open(trash, folder_flags)
    except OSError as exc:
        raise OutsideFolders("trash_unavailable", "this user's trash cannot be used") from exc
    opened: list[int] = []
    try:
        for sub in ("files", "info"):
            with contextlib.suppress(FileExistsError):
                os.mkdir(sub, 0o700, dir_fd=trash_fd)
            try:
                opened.append(os.open(sub, folder_flags, dir_fd=trash_fd))
            except OSError as exc:
                raise OutsideFolders("trash_unavailable", "the trash is not a folder") from exc
        files, info = opened
        if os.fstat(files).st_dev != identity[0]:
            raise OutsideFolders(
                "trash_unavailable", "the file is on another drive than this user's trash"
            )
        exclusive = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec
        for candidate in _trash_names(name):
            try:
                record = os.open(f"{candidate}.trashinfo", exclusive, 0o600, dir_fd=info)
            except FileExistsError:
                continue
            if _peek(candidate, files) is None:
                break
            os.close(record)
            os.unlink(f"{candidate}.trashinfo", dir_fd=info)
        else:
            raise OutsideFolders("trash_unavailable", "no free name in the trash")
        when = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with os.fdopen(record, "w", encoding="utf-8") as handle:
                handle.write(f"[Trash Info]\nPath={quote(original)}\nDeletionDate={when}\n")
            _rename_noreplace(directory, name, files, candidate)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(f"{candidate}.trashinfo", dir_fd=info)
            raise
        moved = os.stat(candidate, dir_fd=files, follow_symlinks=False)
        if (moved.st_dev, moved.st_ino) != identity:
            with contextlib.suppress(OSError):
                _rename_noreplace(files, candidate, directory, name)
                os.unlink(f"{candidate}.trashinfo", dir_fd=info)
            raise OutsideFolders("changed", "the file changed while it was deleted (put back)")
        return str(trash / "files" / candidate)
    finally:
        for fd in opened:
            os.close(fd)
        os.close(trash_fd)


def _mac_trash(original: str, identity: tuple[int, int]) -> str:
    """The system's own Trash (`NSFileManager.trashItemAtURL`, "Put Back" works); the item it
    moved must be the file that was checked."""
    try:
        foundation: Any = __import__("Foundation")
    except ImportError as exc:
        raise OutsideFolders("trash_unavailable", "the Trash needs PyObjC on a Mac") from exc
    url = foundation.NSURL.fileURLWithPath_(original)
    manager = foundation.NSFileManager.defaultManager()
    ok, result, _error = manager.trashItemAtURL_resultingItemURL_error_(url, None, None)
    if not ok or result is None:
        raise OutsideFolders("trash_unavailable", "macOS did not move the file to the Trash")
    where = str(result.path())
    moved = os.lstat(where)
    if (moved.st_dev, moved.st_ino) != identity:
        raise OutsideFolders("changed", f"something else went to the Trash ({where})")
    return where


# -- Windows: move and Recycle Bin ----------------------------------------------------------------


@functools.cache
def _kernel32_more() -> Any:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    k32 = _kernel32()
    k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    k32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.GetVolumePathNameW.restype = wintypes.BOOL
    k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
    k32.GetVolumeNameForVolumeMountPointW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    k32.GetDriveTypeW.restype = wintypes.UINT
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    return k32


def _win_fd(path: str, access: int, share: int, flags: int) -> int:
    """An fd for an existing file or folder on a local disk (`CreateFileW`, long-path form)."""
    import ctypes  # noqa: PLC0415

    winapi: Any = ctypes
    k32 = _kernel32()
    handle = k32.CreateFileW(
        path if path.startswith("\\\\?\\") else "\\\\?\\" + path,
        access,
        share,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise winapi.WinError(winapi.get_last_error())
    try:
        if k32.GetFileType(handle) != _FILE_TYPE_DISK:
            raise OutsideFolders("not_a_file", "only files on a disk are opened")
        fd: int = _msvcrt().open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        k32.CloseHandle(handle)
        raise
    return fd


def _win_leaf(folders: Folders, root: _Root, path: str, access: int, share: int) -> tuple[int, str]:
    """The file itself - never a link it may be (`FILE_FLAG_OPEN_REPARSE_POINT`) -, checked on
    its handle with the write rules. The fd and its real path."""
    fd = _win_fd(path, access, share, _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT)
    try:
        info = os.fstat(fd)
        if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OutsideFolders("link_refused", "links are not followed on this device")
        if stat.S_ISDIR(info.st_mode):
            raise OutsideFolders("not_a_file", "folders are not moved or deleted, only files")
        final = _win_final(fd)
        if final is None:
            raise OutsideFolders("outside_folders", "the path leads to a network or unknown place")
        folders._verify(root, info, final, write=True)
    except BaseException:
        os.close(fd)
        raise
    return fd, final


def _win_folder(folders: Folders, root: _Root, path: str) -> tuple[int, str]:
    """A folder, pinned: open without `FILE_SHARE_DELETE` (it cannot be renamed or replaced
    while held), verified on its handle. The fd and its real path."""
    fd = _win_fd(
        path, _GENERIC_READ, _FILE_SHARE_READ | _FILE_SHARE_WRITE, _FILE_FLAG_BACKUP_SEMANTICS
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a folder", path)
        final = _win_final(fd)
        if final is None:
            raise OutsideFolders("outside_folders", "the path leads to a network or unknown place")
        folders._verify(root, info, final)
    except BaseException:
        os.close(fd)
        raise
    return fd, final


def _win_rename(fd: int, new_path: str) -> None:
    """`SetFileInformationByHandle(FileRenameInfo)` on this handle, never replacing a file."""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    winapi: Any = ctypes
    name = new_path if new_path.startswith("\\\\?\\") else "\\\\?\\" + new_path

    class RenameInfo(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", ctypes.c_wchar * (len(name) + 1)),
        )

    info = RenameInfo(0, None, len(name) * 2, name)
    handle = _msvcrt().get_osfhandle(fd)
    if not _kernel32().SetFileInformationByHandle(
        handle, _FILE_RENAME_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise winapi.WinError(winapi.get_last_error())


def _win_move(
    folders: Folders, sroot: _Root, sparts: list[str], troot: _Root, tparts: list[str]
) -> Moved:
    folder_fd, folder = _win_folder(folders, troot, os.path.join(troot.path, *tparts[:-1]))
    try:
        target = os.path.join(folder, tparts[-1])
        folders._verify_name(troot, target)
        source_fd, source = _win_leaf(
            folders,
            sroot,
            os.path.join(sroot.path, *sparts),
            _GENERIC_READ | _DELETE,  # DELETE: renamed through this very handle
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,  # no FILE_SHARE_DELETE: nobody else renames it
        )
        try:
            if _norm(source) == _norm(target):
                raise FileExistsError(errno.EEXIST, "the target exists", target)
            if os.fstat(source_fd).st_dev != os.fstat(folder_fd).st_dev:
                raise OutsideFolders("cross_device", "a file is moved only within one drive")
            _target_content_refusal(
                lambda n: os.read(source_fd, n), os.fstat(source_fd).st_size, tparts[-1]
            )
            _win_rename(source_fd, target)
            now = _win_final(source_fd)
            try:
                if now is None or _norm(now) != _norm(target):
                    raise OutsideFolders("changed", "the file did not arrive where it should")
                folders._verify(troot, os.fstat(source_fd), now, write=True)
            except OutsideFolders:
                with contextlib.suppress(OSError):
                    _win_rename(source_fd, source)  # back where it was
                raise
            return Moved(source, now)
        finally:
            os.close(source_fd)
    finally:
        os.close(folder_fd)


def _win_volume(path: str) -> tuple[str, str]:
    """The volume root of `path` (`D:\\`) and its GUID (`{...}`, empty when unknown)."""
    import ctypes  # noqa: PLC0415

    k32 = _kernel32_more()
    buffer = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(path, buffer, 1024):
        return "", ""
    root = buffer.value
    guid = ctypes.create_unicode_buffer(128)
    if not k32.GetVolumeNameForVolumeMountPointW(root, guid, 128):
        return root, ""
    found = re.search(r"\{[0-9A-Fa-f-]{36}\}", guid.value)
    return root, found.group(0) if found else ""


def _win_bin_refusal(final: str, size: int) -> str | None:
    """Why the Recycle Bin would not keep this file (Windows would delete it for good), or None:
    not a fixed drive, "delete immediately" (`NoRecycleFiles` policy, `NukeOnDelete`), or larger
    than the bin's size (`MaxCapacity`, MB) - read like Explorer reads them."""
    root, guid = _win_volume(final)
    if not root or _kernel32_more().GetDriveTypeW(root) != _DRIVE_FIXED:
        return "the drive has no Recycle Bin"
    winreg: Any = __import__("winreg")

    def value(hive: Any, key: str, name: str) -> int | None:
        try:
            with winreg.OpenKey(hive, key) as handle:
                found, _kind = winreg.QueryValueEx(handle, name)
        except OSError:
            return None
        return int(found) if isinstance(found, int) else None

    policy = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        if value(hive, policy, "NoRecycleFiles") == 1:
            return "the Recycle Bin is switched off by policy"
    if guid:
        volume = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}"
        if value(winreg.HKEY_CURRENT_USER, volume, "NukeOnDelete") == 1:
            return "the Recycle Bin of this drive deletes files immediately"
        capacity = value(winreg.HKEY_CURRENT_USER, volume, "MaxCapacity")
        if capacity is not None and size > capacity * 1024 * 1024:
            return "the file is larger than this drive's Recycle Bin"
    return None


def _win_delete_pending(fd: int) -> bool:
    """`FILE_STANDARD_INFO.DeletePending` of this handle (True when it cannot be read)."""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    class StandardInfo(ctypes.Structure):
        _fields_ = (
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", wintypes.BOOLEAN),
            ("Directory", wintypes.BOOLEAN),
        )

    info = StandardInfo()
    handle = _msvcrt().get_osfhandle(fd)
    if not _kernel32_more().GetFileInformationByHandleEx(
        handle, _FILE_STANDARD_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return True
    return bool(info.DeletePending)


def _win_trash(folders: Folders, root: _Root, parts: list[str]) -> str:
    from nox.files.recycle import RecycleError, to_recycle_bin  # noqa: PLC0415

    folder_fd, folder = _win_folder(folders, root, os.path.join(root.path, *parts[:-1]))
    try:
        fd, final = _win_leaf(
            folders,
            root,
            os.path.join(root.path, *parts),
            # Attributes only, every sharing mode: this handle takes part in no sharing check,
            # so the shell can move the file into the bin - and it shows where the file went.
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        )
        try:
            if _norm(os.path.dirname(final)) != _norm(folder):
                raise OutsideFolders("changed", "the file changed while it was opened")
            size = os.fstat(fd).st_size
            if size > MAX_TRASH_BYTES:
                raise OutsideFolders("too_large", "a file this large is not put in the trash here")
            refusal = _win_bin_refusal(final, size)
            if refusal is not None:
                raise OutsideFolders("trash_unavailable", refusal)
            failure = ""
            try:
                to_recycle_bin(Path(final))
            except RecycleError as exc:
                failure = str(exc)
            if _win_delete_pending(fd):
                raise OutsideFolders(
                    "trash_failed", "Windows deleted the file instead of recycling"
                )
            now = _win_final(fd)
            if now is None or "\\$RECYCLE.BIN\\" not in now.upper():
                if failure:
                    raise OutsideFolders("trash_failed", failure[:200])
                raise OutsideFolders("changed", "something else went to the Recycle Bin")
            return now
        finally:
            os.close(fd)
    finally:
        os.close(folder_fd)
