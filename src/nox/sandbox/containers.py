"""The fixed set of Windows AppContainers Nox runs workloads in, and which workload gets which.

Every Nox workload runs in an AppContainer of its own class (`nox.sandbox.windows`): shell
commands in `Nox.Sandbox`, every MCP server, skill and skill test in one container of a fixed pool
(`Nox.Sandbox.w000` ... `Nox.Sandbox.w127`). The pool is fixed so that the container SIDs are
known *before* any workload ever ran: the installer's one elevated step puts network rules on
exactly these SIDs (`nox.sandbox.wfp`), and a container Nox creates later is already covered.

An AppContainer SID is a pure function of the container's name (`container_sid`, the same SHA-256
derivation `DeriveAppContainerSidFromAppContainerName` uses), so the SIDs can be computed - and the
rules built and tested - on any platform, without creating a profile.

Which workload gets which slot is decided once, first come first served, and recorded in
`<nox home>/sandbox/container-slots.json` (`ContainerSlots`). A workload's own name never picks
its slot, so no MCP server or skill can name itself into another one's container; two workload
classes never share a slot; a slot is never handed to a second workload class (its profile folder
could still hold what the first one left). A full pool fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
from pathlib import Path

from nox.sandbox.model import SandboxUnavailable

__all__ = [
    "DEFAULT_WORKLOAD",
    "SHELL_CONTAINER",
    "WORKLOAD_SLOTS",
    "ContainerSlots",
    "all_container_names",
    "container_name",
    "container_sid",
    "default_slots_file",
    "slot_container_name",
]

#: The container of shell commands - the name every earlier build used.
SHELL_CONTAINER = "Nox.Sandbox"
#: The workload class of a run that names none: a shell command.
DEFAULT_WORKLOAD = "shell"
#: How many workload classes besides shell commands can have a container of their own.
WORKLOAD_SLOTS = 128


def slot_container_name(slot: int) -> str:
    """The AppContainer name of pool slot `slot`."""
    if not 0 <= slot < WORKLOAD_SLOTS:
        raise ValueError(f"no container slot {slot}")
    return f"{SHELL_CONTAINER}.w{slot:03d}"


def all_container_names() -> tuple[str, ...]:
    """Every container a Nox workload can ever run in: shell's first, then the pool."""
    return (SHELL_CONTAINER, *(slot_container_name(i) for i in range(WORKLOAD_SLOTS)))


def container_name(workload: str, slots: ContainerSlots) -> str:
    """The container of `workload`'s class: shell's for shell commands, a pool slot otherwise."""
    if workload in ("", DEFAULT_WORKLOAD):
        return SHELL_CONTAINER
    return slot_container_name(slots.slot_for(workload))


def container_sid(name: str) -> str:
    """The AppContainer SID of container `name`, computed exactly as Windows derives it.

    `S-1-15-2-` followed by the first 28 bytes of SHA-256 over the lower-cased name in UTF-16LE,
    read as seven little-endian 32-bit sub-authorities. Checked against
    `DeriveAppContainerSidFromAppContainerName` on Windows (`test_wfp`).
    """
    digest = hashlib.sha256(name.lower().encode("utf-16-le")).digest()
    return "S-1-15-2-" + "-".join(str(part) for part in struct.unpack("<7I", digest[:28]))


def default_slots_file() -> Path:
    from nox.paths import nox_home

    return nox_home() / "sandbox" / "container-slots.json"


class ContainerSlots:
    """Which workload class owns which pool slot - assigned once, recorded, never reassigned."""

    def __init__(self, path: Path | None = None, *, size: int = WORKLOAD_SLOTS) -> None:
        self.path = path or default_slots_file()
        self.size = size
        self._lock = threading.Lock()

    def _load(self) -> dict[str, int]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SandboxUnavailable(f"the sandbox container table is unreadable: {exc}") from exc
        try:
            data = json.loads(raw)
            slots = data["slots"]
            table = {str(k): int(v) for k, v in slots.items()}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # Starting over could hand a slot to a second workload class: fail closed instead.
            raise SandboxUnavailable(
                f"the sandbox container table {self.path} is damaged; fix or delete it"
            ) from exc
        if any(not 0 <= v < self.size for v in table.values()) or len(set(table.values())) != len(
            table
        ):
            raise SandboxUnavailable(
                f"the sandbox container table {self.path} is inconsistent; fix or delete it"
            )
        return table

    def _save(self, table: dict[str, int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "slots": table}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def assigned(self) -> dict[str, int]:
        with self._lock:
            return dict(self._load())

    def slot_for(self, workload: str) -> int:
        """The slot of `workload`, assigning the lowest free one on first use."""
        if workload in ("", DEFAULT_WORKLOAD):
            raise ValueError("shell commands have a container of their own, not a pool slot")
        with self._lock:
            table = self._load()
            if workload in table:
                return table[workload]
            used = set(table.values())
            free = next((i for i in range(self.size) if i not in used), None)
            if free is None:
                raise SandboxUnavailable(
                    f"all {self.size} sandbox containers are taken by other workloads; "
                    f"{workload!r} is refused (fail closed)"
                )
            table[workload] = free
            self._save(table)
            return free
