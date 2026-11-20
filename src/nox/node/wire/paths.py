"""Path spellings a node never resolves on the core's behalf - decided on the text alone.

The node's gate refuses a core string that only the file system could resolve (`..`, `~`, 8.3
short names, UNC and device paths) and `safe_fs` refuses the same spellings again when it opens
a path. One definition keeps both from drifting apart. Owns the text rules; opening a path is
`nox.node.device.safe_fs`.
"""

from __future__ import annotations

import re

__all__ = ["is_remote_or_device_path", "names_a_path_alias"]

#: An 8.3 short name (`NOXNOD~1`): another spelling of a folder that text cannot compare.
_SHORT_NAME = re.compile(r"(^|[\\/])[^\\/\s]{1,6}~\d")


def names_a_path_alias(text: str) -> bool:
    """Whether `text` holds a path spelling that only the file system could resolve - `..`
    segments, `~` at the start of a path, 8.3 short names, UNC or device paths. Decided on the
    text alone: the gate never asks the file system about a core string."""
    for word in text.replace('"', " ").replace("'", " ").split():
        if is_remote_or_device_path(word):
            return True
        if word == "~" or word.startswith(("~/", "~\\")):
            return True
        segments = re.split(r"[\\/]", word)
        if ".." in segments and len(segments) > 1:
            return True
        if _SHORT_NAME.search(word):
            return True
    return False


def is_remote_or_device_path(path: str) -> bool:
    """UNC (`\\\\host\\share`, `//host/share`) and Win32 device paths (`\\\\?\\`,
    `\\\\.\\`): never opened or resolved on behalf of the core."""
    text = path.strip()
    return text.startswith(("\\\\", "//")) or text.replace("/", "\\").startswith(
        ("\\\\?\\", "\\\\.\\")
    )
