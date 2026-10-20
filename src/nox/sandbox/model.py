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
* **Fail closed.** If the backend cannot be set up (no `bwrap`), or its guarantees are weaker than
  the Security Model asks and the user has not explicitly accepted that, `run` raises
  `SandboxUnavailable` rather than running the code less confined than promised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "RESERVED_ENV_KEYS",
    "EgressDecl",
    "Guarantees",
    "Limits",
    "Result",
    "SandboxUnavailable",
    "checked_env",
]


#: Environment keys a caller may not set through `env=`: they are what point the child at its
#: work directory and at the egress proxy, so overriding them would undo the confinement (a
#: `HTTPS_PROXY` of the caller's choosing, a `HOME` in the user's profile).
RESERVED_ENV_KEYS: frozenset[str] = frozenset(
    key.upper()
    for key in (
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "SystemRoot",
        "windir",
        "ComSpec",
        "PSModuleAnalysisCachePath",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    )
)


#: The only shape an extra variable may have: upper-case letters, digits and `_`.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
#: Families that change how a dynamic loader, libc, a language runtime, systemd or D-Bus behaves -
#: on either side of the sandbox. Never accepted, whatever a server's configuration asks for.
FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "LD_",
    "DYLD_",
    "SYSTEMD_",
    "DBUS_",
    "MALLOC_",
    "GLIBC_",
    "GCONV",
    "LOC",
    "LC_",
    "NLS",
    "PYTHON",
    "PERL5",
    "PERLLIB",
    "RUBY",
    "JAVA_TOOL",
    "JDK_JAVA",
    "_JAVA",
    "GIT_",
    "BASH_",
    "XDG_",
    "SSL_",
    "OPENSSL_",
)
#: Single names with the same effect.
FORBIDDEN_ENV_NAMES: frozenset[str] = frozenset(
    {
        "NODE_OPTIONS",
        "NODE_PATH",
        "HOSTALIASES",
        "RES_OPTIONS",
        "LOCALDOMAIN",
        "ENV",
        "IFS",
        "SHELLOPTS",
        "PS4",
        "PROMPT_COMMAND",
        "TZDIR",
        "LANGUAGE",
    }
)
#: Total size of the extra environment (it travels through a pipe on Linux).
MAX_EXTRA_ENV_BYTES = 32 * 1024


def checked_env(env: dict[str, str] | None) -> dict[str, str]:
    """The caller's extra environment (an MCP server's or a skill's settings), checked against an
    allowlist: an upper-case name (`^[A-Z][A-Z0-9_]{0,63}$`) outside the families that steer a
    loader or a runtime (`FORBIDDEN_ENV_PREFIXES`, `FORBIDDEN_ENV_NAMES`), no reserved key, no
    NUL in a value. Anything else is a `ValueError` - refused, never silently dropped."""
    if not env:
        return {}
    clash = sorted(key for key in env if key.upper() in RESERVED_ENV_KEYS)
    if clash:
        raise ValueError(f"the sandbox owns these environment variables: {clash}")
    bad = sorted(
        key
        for key in env
        if not _ENV_NAME.match(key)
        or key in FORBIDDEN_ENV_NAMES
        or key.startswith(FORBIDDEN_ENV_PREFIXES)
    )
    if bad:
        raise ValueError(f"invalid or not allowed environment variable names: {bad}")
    values = {str(k): str(v) for k, v in env.items()}
    if any(chr(0) in value for value in values.values()):
        raise ValueError("an environment value contains a NUL character")
    if sum(len(k) + len(v) + 2 for k, v in values.items()) > MAX_EXTRA_ENV_BYTES:
        raise ValueError("the extra environment is too large")
    return values


class SandboxUnavailable(RuntimeError):  # noqa: N818 - a state, not an error suffix, by design
    """The sandbox could not be set up, so nothing ran. Callers treat this as a hard refusal."""


@dataclass(frozen=True, slots=True)
class EgressDecl:
    """What network a run is allowed to reach, as a request to the egress proxy.

    `hosts` are `host` or `host:port` entries in the same grammar as `security.egress_allowlist`;
    `"*"` is any public host on any port (see `nox.sandbox`), never the LAN or loopback.
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
    #: For a long-running process (`Sandbox.start`) this is the size of the stderr ring buffer.
    max_output_bytes: int = 64 * 1024
    #: Longest single stdout line a long-running process (`Sandbox.start`) may write - one
    #: JSON-RPC message of an MCP stdio server. Longer lines raise `MessageTooLarge`.
    max_message_bytes: int = 8 * 1024 * 1024


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
