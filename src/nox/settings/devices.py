"""The audio output devices the Settings page offers for Nox's voice.

The devices are enumerated where the audio stack lives - the voice worker (`voice.devices.list`)
- and, when no voice worker is running, in this process as a fallback. `config.get` refreshes the
list before it describes the settings, so the page shows a choice of what is plugged in now
rather than a text field to type a device name into.

`COMPUTED_CHOICES` in `nox.settings.schema` is synchronous, so the last list is kept here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from nox.core.logging import get_logger

log = get_logger(__name__)

#: How long `config.get` waits for the voice worker to list its devices.
LIST_TIMEOUT_S = 3.0

WorkerRequest = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class OutputDeviceCatalog:
    """The output device names last seen, or None when none could be listed."""

    def __init__(self) -> None:
        self._names: list[str] | None = None

    def names(self) -> list[str] | None:
        return None if self._names is None else list(self._names)

    def set(self, names: list[str] | None) -> None:
        self._names = None if names is None else sorted(set(names), key=str.casefold)

    async def refresh(self, ask_worker: WorkerRequest | None) -> list[str] | None:
        """Ask the voice worker; fall back to enumerating here; keep the old list on failure."""
        names: list[str] | None = None
        if ask_worker is not None:
            try:
                reply = await asyncio.wait_for(ask_worker("voice.devices.list", {}), LIST_TIMEOUT_S)
                names = [str(n) for n in reply.get("outputs", [])]
            except Exception as exc:  # noqa: BLE001 - a busy worker must not break config.get
                log.info("settings.devices_worker_unavailable", error=type(exc).__name__)
        if names is None:
            names = await asyncio.to_thread(_local_output_devices)
        if names is not None:
            self.set(names)
        return self.names()


def _local_output_devices() -> list[str] | None:
    try:
        from nox.voice.audio import output_device_names  # noqa: PLC0415 - optional audio stack

        return output_device_names()
    except Exception as exc:  # noqa: BLE001 - no PortAudio here: nothing to offer
        log.info("settings.devices_unavailable", error=type(exc).__name__)
        return None


#: The one catalogue of this process, read by `nox.settings.schema.COMPUTED_CHOICES`.
OUTPUT_DEVICES = OutputDeviceCatalog()


def device_choices(current: str) -> list[str] | None:
    """The choices for a device setting: the system default (""), every listed device, and the
    current value if it is not among them (a name fragment typed by hand, or a device that is
    unplugged right now) - so saving other settings never fails over it. None when nothing could
    be listed: the setting then stays a text field."""
    names = OUTPUT_DEVICES.names()
    if names is None:
        return None
    choices = ["", *names]
    if current and current not in choices:
        choices.append(current)
    return choices
