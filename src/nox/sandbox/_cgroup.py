"""Memory and process ceilings for the Linux sandbox from a delegated cgroup v2 subtree.

On the Raspberry Pi the core runs as a systemd *system* service under its own unprivileged user.
That user may not create transient scopes (`systemd-run --scope` needs polkit/root, and
`systemd-run --user` needs a user manager plus `/run/user`, which `ProtectHome=` hides from the
service). systemd's answer for a service that manages its own children is `Delegate=yes`: the
service owns its cgroup subtree. `nox.service` sets that, plus `NOX_CGROUP_DELEGATED=1` so the core
knows it may use it (a cgroup that merely *looks* writable - a desktop session scope - is never
touched without it).

How it is used, following systemd's delegation rules (cgroup v2 "no internal processes"):

* at start-up the processes of the service cgroup move into a leaf `nox-core`, and the `memory`
  and `pids` controllers are enabled for the subtree;
* each confined process gets its own `sbx-<id>` cgroup with `memory.max`, `memory.swap.max = 0`
  (a Pi with swap would otherwise page instead of hitting the ceiling) and `pids.max`;
* the process is started by `/bin/sh` that writes its own PID into that cgroup and then `exec`s
  `bwrap` - so the whole tree is inside from its first instruction, no race;
* afterwards `cgroup.kill` ends whatever is left and the cgroup is removed.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from nox.core.logging import get_logger
from nox.sandbox.model import Limits

log = get_logger(__name__)

__all__ = ["DELEGATED_ENV", "DelegatedCgroups", "own_cgroup"]

DELEGATED_ENV = "NOX_CGROUP_DELEGATED"
_ROOT = Path("/sys/fs/cgroup")
_CORE_LEAF = "nox-core"
_CONTROLLERS = ("memory", "pids")
#: The trampoline: `$0` is the run's cgroup, `"$@"` the bwrap command line.
_ENTER = 'echo $$ > "$0/cgroup.procs" && exec "$@"'


def own_cgroup() -> Path | None:
    """This process's cgroup v2 directory, or None (cgroup v1 / hybrid, or no /proc)."""
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            return _ROOT / line[3:].lstrip("/")
    return None


class DelegatedCgroups:
    """Per-run cgroups below the service's delegated cgroup (see the module docstring)."""

    def __init__(self, base: Path) -> None:
        self.base = base

    @classmethod
    def setup(cls) -> DelegatedCgroups | None:
        """Prepare the delegated subtree, or None when there is none to use (then no ceiling)."""
        if os.environ.get(DELEGATED_ENV) != "1":
            return None
        base = own_cgroup()
        if base is None:
            return None
        if base.name == _CORE_LEAF:  # set up before, by an earlier sandbox in this process
            base = base.parent
        try:
            available = (base / "cgroup.controllers").read_text(encoding="ascii").split()
            missing = [c for c in _CONTROLLERS if c not in available]
            if missing:
                raise OSError(f"controllers not delegated: {missing}")
            leaf = base / _CORE_LEAF
            leaf.mkdir(exist_ok=True)
            for pid in (base / "cgroup.procs").read_text(encoding="ascii").split():
                try:
                    (leaf / "cgroup.procs").write_text(pid, encoding="ascii")
                except ProcessLookupError:
                    continue  # it ended meanwhile
            enable = " ".join(f"+{c}" for c in _CONTROLLERS)
            (base / "cgroup.subtree_control").write_text(enable, encoding="ascii")
        except OSError as exc:
            log.warning("sandbox.cgroup_unavailable", cgroup=str(base), error=str(exc))
            return None
        return cls(base)

    def create(self, limits: Limits) -> Path:
        """A fresh cgroup with the run's ceilings. `OSError` when it cannot be made."""
        path = self.base / f"sbx-{uuid.uuid4().hex[:12]}"
        path.mkdir()
        try:
            (path / "memory.max").write_text(str(limits.memory_bytes), encoding="ascii")
            swap = path / "memory.swap.max"
            if swap.exists():
                swap.write_text("0", encoding="ascii")
            (path / "pids.max").write_text(str(limits.max_processes), encoding="ascii")
        except OSError:
            self.remove(path)
            raise
        return path

    @staticmethod
    def wrap(path: Path, cmd: list[str]) -> list[str]:
        """`cmd`, started inside `path` (the trampoline joins the cgroup, then execs)."""
        return ["/bin/sh", "-c", _ENTER, str(path), *cmd]

    @staticmethod
    def kill(path: Path) -> None:
        """Kill every process left in the cgroup (Linux 5.14+; older kernels rely on bwrap)."""
        try:
            (path / "cgroup.kill").write_text("1", encoding="ascii")
        except OSError:
            pass

    @classmethod
    def remove(cls, path: Path, *, wait_s: float = 5.0) -> None:
        """Kill what is left and remove the cgroup; retried while the last processes exit."""
        cls.kill(path)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                path.rmdir()
                return
            except FileNotFoundError:
                return
            except OSError:
                if time.monotonic() >= deadline:
                    log.warning("sandbox.cgroup_left_behind", cgroup=str(path))
                    return
                time.sleep(0.05)
