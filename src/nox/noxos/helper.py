"""The client side of the Nox-OS root helper's socket (`/run/nox-os/helper/helper.sock`).

One request per connection: lines in, one JSON line out (the helper:
`os/pi-gen/stage-nox/03-hardening/files/nox_os_helper.py`). The helper answers only the main
process of nox.service; everything here is the transport, the requests themselves belong to
`nox.noxos.install` (remote maintenance) and `nox.noxos.claim` (the claim).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

__all__ = [
    "HELPER_SOCKET",
    "HELPER_TIMEOUT_S",
    "HelperCall",
    "HelperError",
    "OS_RELEASE",
    "unix_helper",
]

HELPER_SOCKET = Path("/run/nox-os/helper/helper.sock")
OS_RELEASE = Path("/etc/nox-os-release")
#: The helper starts sshd and a timer and reloads the firewall: give it time on a Pi Zero.
HELPER_TIMEOUT_S = 90.0
MAX_ANSWER_BYTES = 64 * 1024

#: One call to the root helper: request lines in, its JSON answer out.
HelperCall = Callable[[list[str]], Awaitable[dict[str, Any]]]


class HelperError(Exception):
    """The helper could not be reached, or refused (`refused`: its message is safe to show)."""

    def __init__(self, message: str, *, refused: bool = False, code: str = "") -> None:
        super().__init__(message)
        self.refused = refused
        self.code = code


def unix_helper(path: Path = HELPER_SOCKET, timeout_s: float = HELPER_TIMEOUT_S) -> HelperCall:
    async def call(lines: list[str]) -> dict[str, Any]:
        if sys.platform == "win32":
            raise HelperError("the Nox-OS helper exists on Nox-OS only")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path), limit=MAX_ANSWER_BYTES), 5.0
            )
        except (OSError, TimeoutError) as exc:
            raise HelperError(f"the Nox-OS helper is not reachable ({type(exc).__name__})") from exc
        try:
            writer.write(("\n".join(lines) + "\n").encode("ascii"))
            await writer.drain()
            writer.write_eof()
            raw = await asyncio.wait_for(reader.readline(), timeout_s)
        except (OSError, TimeoutError, ValueError) as exc:
            raise HelperError(f"the Nox-OS helper did not answer ({type(exc).__name__})") from exc
        finally:
            writer.close()
        try:
            answer = json.loads(raw)
        except ValueError as exc:
            raise HelperError("the Nox-OS helper gave an answer that cannot be read") from exc
        if not isinstance(answer, dict):
            raise HelperError("the Nox-OS helper gave an answer that cannot be read")
        if answer.get("ok") is not True:
            raise HelperError(
                str(answer.get("error", "refused"))[:200],
                refused=True,
                code=str(answer.get("code", ""))[:40],
            )
        return answer

    return call
