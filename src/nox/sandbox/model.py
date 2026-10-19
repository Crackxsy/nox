"""The OS-agnostic sandbox contract: what a caller declares, and what comes back.

`Sandbox.run(argv, cwd, env, egress, limits)` is the whole surface another part of Nox needs - the
shell tool, a self-built skill's tests, a foreign MCP server. The backend (Windows job object +
low-integrity/AppContainer token, or Linux `bwrap` + `systemd-run`) is chosen per OS and hidden
behind it, so callers never branch on the platform.

Three rules the contract makes explicit, because they are the point of the sandbox:

* **Network is default-deny.** `EgressDecl.NONE` means no network at all. Any other declaration
  reaches the outside world *only* through Nox's loopback egress proxy, which applies the same
  egress policy as the core and always refuses the Home Assistant host and the LAN. A declaration
  is a request to the proxy, never a hole in it.
* **The work directory is the only writable place.** Everything else the process can see is
  read-only; the keyring and the user's files are not reachable at all on a backend that supports
  it (AppContainer, bwrap), and read-only-but-reachable on the weaker Windows fallback, which
  `Sandbox.guarantees()` reports honestly.
* **Fail closed.** If the backend cannot be set up (no `bwrap`, no AppContainer and the firewall
  rule missing), `run` raises `SandboxUnavailable` rather than running the code unconfined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "EgressDecl",
    "Guarantees",
    "Limits",
    "Result",
    "SandboxUnavailable",
]


class SandboxUnavailable(RuntimeError):  # noqa: N818 - a state, not an error suffix, by design
    """The sandbox could not be set up, so nothing ran. Callers treat this as a hard refusal."""


@dataclass(frozen=True, slots=True)
class EgressDecl:
    """What network a run is allowed to reach, as a request to the egress proxy.

    `hosts` are `host` or `host:port` entries in the same grammar as `security.egress_allowlist`.
    An empty tuple (the default, `EgressDecl.NONE`) means no network at all. The proxy still vetoes
    anything its own policy forbids - the Home Assistant host and LAN ranges above all - so a
    declaration can only ever narrow what the proxy already permits, never widen it.
    """

    hosts: tuple[str, ...] = ()

    @property
    def wants_network(self) -> bool:
        return bool(self.hosts)


EgressDecl.NONE = EgressDecl()  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource ceilings for one run. The backend enforces what its OS can."""

    #: Wall-clock seconds before the process tree is killed.
    timeout_s: float = 60.0
    #: Hard memory ceiling for the whole tree (job memory / MemoryMax).
    memory_bytes: int = 1024 * 1024 * 1024
    #: Most processes the tree may hold at once (ActiveProcessLimit / TasksMax).
    max_processes: int = 32
    #: Bytes of stdout/stderr captured before the rest is dropped (the model never needs more).
    max_output_bytes: int = 64 * 1024


@dataclass(frozen=True, slots=True)
class Result:
    """The outcome of one run. `stdout`/`stderr` are already truncated to the output limit."""

    exit_code: int | None
    stdout: str
    stderr: str
    #: True when the run was killed for exceeding `Limits.timeout_s`.
    timed_out: bool = False
    #: True when stdout or stderr was longer than the limit and the tail was dropped.
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True, slots=True)
class Guarantees:
    """What a backend actually enforces on this machine, so the core can be honest about it."""

    backend: str
    #: The process cannot write outside the work directory.
    confines_writes: bool
    #: The process cannot read the user's files (stronger than write confinement).
    confines_reads: bool
    #: The keyring / OS credential store is unreachable from inside.
    hides_secrets: bool
    #: Outbound network is blocked unless it goes through the Nox egress proxy.
    confines_network: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def strong(self) -> bool:
        """Every hard guarantee the Security Model asks of a Wave 1 sandbox holds."""
        return (
            self.confines_writes
            and self.hides_secrets
            and self.confines_network
            and self.confines_reads
        )


class SandboxMode(StrEnum):
    """Which backend variant is in use; reported by `Guarantees.backend` for the dashboard."""

    WINDOWS_APPCONTAINER = "windows-appcontainer"
    WINDOWS_LOW_IL = "windows-low-il"
    LINUX_BWRAP = "linux-bwrap"
    UNAVAILABLE = "unavailable"
