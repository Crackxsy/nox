"""The folders a device allows (`Folders`), the path text rules of one open and the refusal of a
folder that is too broad."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from nox.node.device.safe_fs.core import _WINDOWS, OutsideFolders, _plain, _within
from nox.node.device.safe_fs.formats import execution_trigger, write_refusal
from nox.node.device.safe_fs.secret_places import _protected
from nox.node.device.safe_fs.workspace import marked_above
from nox.node.wire.paths import is_remote_or_device_path, names_a_path_alias

#: Win32 device names, with or without an extension (`nul.txt` is still NUL on most Windows).
_DEVICE_NAME = re.compile(
    r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.I
)


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
