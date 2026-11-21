"""Repositories and app workspaces, which nothing writes into: the markers that name them and the
look above a path (by text) or inside an opened folder (on its handle)."""

from __future__ import annotations

import functools
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nox.node.device.safe_fs.core import OutsideFolders, _peek, _plain, _within

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
