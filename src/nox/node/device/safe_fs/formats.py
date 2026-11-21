"""What a node may write at all, decided on the path's text: the allowed text formats in two tiers,
the formats that are never written, the configuration names and the execution triggers
(`execution_trigger`). Nothing here opens a file."""

from __future__ import annotations

import fnmatch
import re

from nox.node.device.safe_fs.core import OutsideFolders

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
