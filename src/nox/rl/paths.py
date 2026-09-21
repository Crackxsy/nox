"""Resolve the real, possibly OneDrive-redirected, Rocket League replay folder.

The Windows Documents special folder is asked for the real path rather than assuming a drive
letter, so a redirected Documents folder still resolves. Off Windows (and in tests) this falls back
to `%USERPROFILE%/Documents`.
"""

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
    """`Documents/My Games/Rocket League/TAGame/DemosEpic`, under the real Documents folder.

    The literal path differs per machine, which is why it is resolved rather than configured.
    """
    return documents_dir().joinpath(*_REPLAY_SUBPATH)
