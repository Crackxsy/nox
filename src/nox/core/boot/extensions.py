"""Importing and installing the release extensions.

Each extension is a module `nox.<name>.install` with an `install(core)` function; see
`nox.core.extension` for the contract. The import is the expensive half - one of them pulls in an
image-processing stack and takes seconds on a cold start - and it is plain blocking work, so it
runs in a worker thread. `install(core)` itself stays on the loop, because it wires bus
subscriptions and creates tasks. Without the split the loop froze for about ten seconds here and
the supervisor counted the boot as missed heartbeats.

A failing extension is reported as unavailable and never aborts the boot.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Sequence
from typing import Any

from nox.core.extension import ExtensionRuntime
from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["DEFAULT_EXTENSIONS", "install_extensions", "stop_extensions"]

#: The extensions the core installs, in installation order. A name here is a module
#: `nox.<name>.install`; whether an extension does anything is its own decision, taken from the
#: configuration it reads.
DEFAULT_EXTENSIONS: tuple[str, ...] = (
    "sensors",
    "memory",
    "health",
    "proactive",
    "pm",
    "rl",
    "clips",
    "creative",
    "settings",
    "remote",
)


async def install_extensions(
    core: Any, names: Sequence[str] = DEFAULT_EXTENSIONS
) -> dict[str, ExtensionRuntime]:
    """Install each named extension onto `core`; returns each one's runtime, or None."""
    installed: dict[str, ExtensionRuntime] = {}
    for name in names:
        try:
            module = await asyncio.to_thread(importlib.import_module, f"nox.{name}.install")
            installed[name] = module.install(core)
            log.info("extension.installed", extension=name)
        except Exception as exc:  # noqa: BLE001 - one extension degrades, the core still starts
            log.error("extension.failed", extension=name, error=f"{type(exc).__name__}: {exc}")
            installed[name] = None
        await asyncio.sleep(0)  # one extension per loop iteration
    return installed


async def stop_extensions(runtimes: dict[str, ExtensionRuntime], *, timeout_s: float = 2.0) -> None:
    """Stop the extensions in reverse installation order, with a budget each."""
    for name, runtime in reversed(list(runtimes.items())):
        stop = getattr(runtime, "stop", None)
        if stop is None:
            continue
        try:
            result = stop()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - shutdown continues past a stubborn extension
            log.warning(
                "extension.stop_failed", extension=name, error=f"{type(exc).__name__}: {exc}"
            )
