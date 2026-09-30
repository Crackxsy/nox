"""`install(core)`: registers the file tools when a folder has been configured for them.

Returns `None`: nothing to shut down. There is no IPC surface either - the roots are edited through
`config.set` like every other setting, which is what gives them the same validation, the same audit
entry and the same live apply. A second write path would be a second place where a folder could be
added that the configuration itself would reject.

The tools are registered even when `files.roots` is empty, and that is deliberate. A model that has
`file.list` and gets "no folder is configured for file access; add one to files.roots" can tell the
user something useful. A model with no file tool at all can only say it cannot read files, which is
the wrong answer to "why can't you look in my Downloads folder".
"""

from __future__ import annotations

from typing import Any, Protocol

from nox.core.config import NoxConfig
from nox.core.extension import ExtensionRuntime
from nox.core.logging import get_logger
from nox.files.recycle import available as recycle_available
from nox.files.tools import register_file_tools
from nox.tools.registry import ToolRegistry

log = get_logger(__name__)

__all__ = ["install"]


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test needs no real core)."""

    config: NoxConfig
    tool_registry: ToolRegistry


def install(core: CoreLike) -> ExtensionRuntime:
    config = core.config.files
    if not config.enabled:
        log.info("files.disabled", detail="no file tools registered")
        return None

    settings: Any = lambda: core.config.files  # noqa: E731 - read per call, so a live apply lands
    register_file_tools(core.tool_registry, settings)
    log.info(
        "files.installed",
        roots=len(config.roots),
        delete=config.delete_to_recycle_bin and recycle_available(),
    )
    return None
