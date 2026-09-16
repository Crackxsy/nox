"""Resolve the real (OneDrive-redirected) Rocket League replay folder via the Windows Documents
special folder (ST-12-02 AC: "resolves the real path via the Windows special-folder API, not a
hardcoded drive letter"). Falls back to `%USERPROFILE%/Documents` off-Windows/in tests."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

_REPLAY_SUBPATH = ("My Games", "Rocket League", "TAGame", "DemosEpic")

# CSIDL_PERSONAL (Documents), matching the constant used elsewhere for special-folder lookups.
_CSIDL_PERSONAL = 0x0005
_SHGFP_TYPE_CURRENT = 0


def documents_dir() -> Path:
    """The user's real Documents folder, following OneDrive Known Folder redirection when the
    registry points there (`SHGetFolderPathW`, read-only Win32 API call - no injection, no
    memory read of another process)."""
    if os.name == "nt":
        buf = ctypes.create_unicode_buffer(1024)
        result = ctypes.windll.shell32.SHGetFolderPathW(
            None, _CSIDL_PERSONAL, None, _SHGFP_TYPE_CURRENT, buf
        )
        if result == 0 and buf.value:
            return Path(buf.value)
    return Path(os.path.expanduser("~/Documents"))


def default_replay_folder() -> Path:
    """`Documents/My Games/Rocket League/TAGame/DemosEpic`, resolved from the real Documents
    location (Spec §6.5/§15 - the literal path differs per machine)."""
    return documents_dir().joinpath(*_REPLAY_SUBPATH)
