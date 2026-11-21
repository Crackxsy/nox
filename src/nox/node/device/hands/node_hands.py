"""`NodeHands`: the shell of a device, run in the sandbox, and the sandbox the node builds by
default."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nox.core.logging import get_logger
from nox.node.device.safe_fs.folders import Folders
from nox.node.device.safe_fs.secret_places import secret_locations
from nox.node.device.tools import NodeRefusal
from nox.sandbox import EgressDecl, Limits, Sandbox, SandboxUnavailable

log = get_logger(__name__)


#: Below the agent's 30 s ceiling for a call with a side effect, so the sandbox ends the run
#: itself and reports `timed_out` instead of the call being cut off.
RUN_TIMEOUT_S = 25.0


@dataclass
class NodeHands:
    """What the hands need from the node: the allowed folders (from the confirmed manifest, read
    per call) and the sandbox (made once, lazily; None when this device has none)."""

    folders: Callable[[], list[str]]
    protected: tuple[Path, ...]
    sandbox: Callable[[], Sandbox | None]
    trusted_dirs: tuple[str, ...] = ()
    timeout_s: float = RUN_TIMEOUT_S
    _cached: list[Sandbox | None] = field(default_factory=list)

    def resolved(self) -> Folders:
        from nox.paths import nox_home  # noqa: PLC0415

        # Nox's own folder too: a core on the same device keeps its database and config there.
        return Folders.build(self.folders(), [*self.protected, nox_home(), *secret_locations()])

    def exposed(self) -> str | None:
        """Why this device's sandbox would show something it must not: the node's folder, Nox's
        folder or a secret store under one of the system folders bwrap binds read-only."""
        if not sys.platform.startswith("linux"):
            return None
        from nox.paths import nox_home  # noqa: PLC0415
        from nox.sandbox.linux import visible_system_paths  # noqa: PLC0415

        private = [*self.protected, nox_home(), *secret_locations()]
        for place in private:
            real = os.path.realpath(place)
            for shown in visible_system_paths():
                if real == shown or real.startswith(shown.rstrip("/") + "/"):
                    return f"{real} lies under {shown}, which the sandbox shows read-only"
        return None

    async def confined(self) -> Sandbox:
        """This device's sandbox with strong guarantees, or `NodeRefusal("sandbox_unavailable")`."""
        if not self._cached:
            self._cached.append(await asyncio.to_thread(self.sandbox))
        sandbox = self._cached[0]
        if sandbox is None:
            raise NodeRefusal("sandbox_unavailable")
        problem = self.exposed()
        if problem is not None:
            log.error("node.sandbox_exposes_private_folder", detail=problem)
            raise NodeRefusal("sandbox_unavailable")
        try:
            await asyncio.to_thread(sandbox.check_usable)
        except SandboxUnavailable as exc:
            raise NodeRefusal("sandbox_unavailable") from exc
        return sandbox

    async def run(
        self, argv: list[str], *, egress: tuple[str, ...], workload: str
    ) -> dict[str, Any]:
        sandbox = await self.confined()
        try:
            result = await sandbox.run(
                argv,
                egress=EgressDecl(hosts=egress),
                limits=Limits(timeout_s=self.timeout_s),
                workload=workload,
            )
        except SandboxUnavailable as exc:
            raise NodeRefusal("sandbox_unavailable") from exc
        return {
            "ok": result.ok,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }


def default_sandbox() -> Sandbox | None:
    """`make_sandbox()` with the strong-guarantee rule; None where this OS has none."""
    from nox.sandbox import make_sandbox  # noqa: PLC0415 - heavy, only when a hand runs

    try:
        return make_sandbox(accept_weak=False)
    except SandboxUnavailable:
        return None
