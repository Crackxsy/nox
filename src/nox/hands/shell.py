"""`shell.run`: run a command in the sandbox, with the command-pattern grant the spec asks for.

Two input shapes (both from the spec's "argv oder Befehlszeile"):

* **argv** - a list already split into program and arguments. It is run *without a shell*, so none
  of `;`, `&&`, `|`, `>` means anything; it classifies by its program and subcommand.
* **command** - one line for the OS shell (`powershell -Command` / `bash -c`). The shell interprets
  operators, so a command line is **always complex**: it always asks and can never match a
  remembered pattern (Security Model §1).

Classification decides the permission target, which is how the engine tells the cases apart
without parsing commands itself:

* `readonly:<program>` - a vetted read-only command (`CURATED_READONLY`) given as argv, resolved to
  a trusted system directory, with no network declared, and only while the sandbox is the strong
  kind (AppContainer / bwrap) - never in the Windows low-integrity fallback, which can read the
  user's files. The engine then allows it without a confirmation (confirmation fatigue is a UX
  bug).
* `cmd:<path>#<sha> <subcommand>` - a simple command; HIGH, CONFIRM, and "remember" stores exactly
  this target as a `cmd:` grant for the session. It names the *resolved executable* and a hash of
  its contents, so a remembered `git push` never covers a different `git.exe` (a download folder,
  a planted copy) or the same path after the file changed.
* `complex:<sha>` - a command line, any interpreter or launcher (below), a program given with a
  path, or a program not found in the trusted directories; HIGH, CONFIRM, never remembered.

**Interpreters and launchers are always complex.** A `cmd:` grant covers one program's subcommand
with any further arguments - right for `git push origin` vs `git push upstream`, wrong for a
program whose arguments *are* the program: a remembered `python a.py` would cover `python evil.py`,
a remembered `env X=1 foo` any other program. So in argv form every interpreter (`python`, `node`,
`pwsh`, `cmd`, `bash`, ...) and every wrapper that starts another program (`env`, `xargs`, `nice`,
`timeout`, `sudo`, `runas`, `start`, `npx`, `uvx`, ...) is complex - confirmed each time, never
remembered - and so are `find` with an `-exec`-like action and `git` with an option or subcommand
that runs a configured command (`-c`, `--exec-path`, `rebase -x`, `submodule foreach`, ...).

A declared network (`egress`) is part of the target (`... egress=a.example,b.example`), so the
confirmation shows it, a grant remembered without egress never covers a run with egress, and the
tool is treated as a network tool by the privacy rules (OFFLINE/PRIVATE refuse it).

Where programs come from: argv0 must be a **bare name** (`whoami`, `git`), looked up only in fixed
trusted directories - `System32` (and Windows PowerShell's folder) on Windows, `/usr/bin` and
`/bin` on Linux - plus any folder the user added to `hands.trusted_program_dirs` (for example the
Git install). Anything with a path separator, a drive, or not found there is complex. What runs is
the resolved absolute path, so what was confirmed is what runs.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, model_validator

from nox.core.logging import get_logger
from nox.sandbox import EgressDecl, Limits, Result, Sandbox, SandboxMode, SandboxUnavailable
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = [
    "CURATED_READONLY",
    "ProgramResolver",
    "ShellClass",
    "classify",
    "default_trusted_dirs",
    "register_shell_tools",
]

#: Any ASCII control character or DEL. An argv0 that carries one is not a real program name; a
#: free-form command line is already always complex, so this guards the argv path.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
#: A bare program name: no separator, no drive, no wildcard, no quote.
_BARE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
#: Interpreters: what they run is in their arguments, so no subcommand grant can describe it.
_INTERPRETERS = frozenset(
    {
        "python",
        "python3",
        "pythonw",
        "py",
        "pyw",
        "node",
        "deno",
        "bun",
        "bash",
        "sh",
        "dash",
        "ksh",
        "zsh",
        "fish",
        "busybox",
        "pwsh",
        "powershell",
        "cmd",
        "perl",
        "ruby",
        "php",
        "lua",
        "tclsh",
        "wish",
        "osascript",
        "wscript",
        "cscript",
        "mshta",
        "rundll32",
        "regsvr32",
        "msbuild",
        "installutil",
        "java",
        "dotnet",
        "wsl",
    }
)
#: Wrappers that start another program named in their arguments.
_LAUNCHERS = frozenset(
    {
        "env",
        "xargs",
        "nice",
        "ionice",
        "nohup",
        "timeout",
        "stdbuf",
        "setsid",
        "chroot",
        "unshare",
        "nsenter",
        "flock",
        "watch",
        "time",
        "exec",
        "sudo",
        "doas",
        "su",
        "pkexec",
        "runas",
        "start",
        "schtasks",
        "at",
        "forfiles",
        "npx",
        "pnpx",
        "bunx",
        "uvx",
        "pipx",
        "conhost",
        "explorer",
    }
)
#: `find` actions that run (or delete) something.
_FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fls"})
#: git options and subcommands that run a configured or given command.
_GIT_EXEC_OPTIONS = (
    "-c",
    "--config-env",
    "--exec-path",
    "--exec",
    "-x",
    "--upload-pack",
    "--receive-pack",
    "--extcmd",
    "--tool",
)
_GIT_EXEC_SUBCOMMANDS = frozenset(
    {"submodule", "filter-branch", "filter-repo", "difftool", "mergetool", "bisect", "web--browse"}
)
#: Sandboxes strong enough for the no-confirm read-only exception (they cannot read user files).
_READONLY_MODES = frozenset({SandboxMode.WINDOWS_APPCONTAINER, SandboxMode.LINUX_BWRAP})


def _is_inline_flag(token: str) -> bool:
    """Whether `token` tells an interpreter to run inline code (so the rest is opaque).

    Covers cmd's `/c` and `/k`; the stop-parsing `--%`; PowerShell's `-Command`/`-EncodedCommand`
    and every abbreviation of them (`-c`, `-co`, ..., `-e`, `-en`, `-enc`, and `-ec`); `--eval`; and
    any POSIX short-option cluster that contains `c` (`-c`, `-lc`, `-ic`), how bash/sh/zsh read
    inline code. Being generous is safe: a false positive only means one extra confirmation.
    """
    t = token.lower()
    if t in ("/c", "/k", "--%", "--eval", "-ec"):
        return True
    if t.startswith("-") and not t.startswith("--"):
        body = t[1:]
        if not body:
            return False
        if "command".startswith(body) or "encodedcommand".startswith(body):
            return True  # -c..-command, -e..-encodedcommand (all prefixes)
        if body.isalpha() and "c" in body:
            return True  # a short-option cluster containing c: -lc, -ic, -c
    return False


class ShellClass(StrEnum):
    READONLY = "readonly"
    SIMPLE = "simple"
    COMPLEX = "complex"


@dataclass(frozen=True, slots=True)
class Classification:
    kind: ShellClass
    #: `program subcommand`, the human-readable unit (empty for complex).
    pattern: str
    #: The permission target the engine sees (`readonly:`/`cmd:`/`complex:` prefixed).
    target: str
    #: What runs: for argv, the resolved absolute program path followed by the arguments.
    argv: tuple[str, ...]
    #: True when the line must go through an OS shell (the `command` form), not argv directly.
    needs_shell: bool


#: Vetted read-only commands: {program: allowed argument shapes}. `None` = any arguments, `()` =
#: no arguments at all. Only programs that observe and never change the machine or reach the
#: network, and that exist as real executables (argv mode runs no shell, so a cmdlet or a cmd
#: builtin like `dir` could never run here). `git` is deliberately absent: its repository config
#: can run code (`core.fsmonitor`, pagers, textconv, `diff.external`), several "read" subcommands
#: write files (`--output`) or read any file (`diff --no-index`), so it always asks.
CURATED_READONLY: dict[str, tuple[str, ...] | None] = {
    # Windows (System32)
    "whoami": None,
    # Linux (/usr/bin, /bin)
    "df": None,
    "free": None,
    "uptime": None,
    "uname": None,
    "ls": None,
    "pwd": (),
    # Both. `hostname NAME` would set the name on Linux, so only the bare form.
    "hostname": (),
}


def default_trusted_dirs() -> tuple[str, ...]:
    """Where a bare program name may be found: system directories only, never the user's PATH."""
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return (
            os.path.join(root, "System32"),
            os.path.join(root, "System32", "WindowsPowerShell", "v1.0"),
        )
    return ("/usr/bin", "/bin")


@dataclass(frozen=True, slots=True)
class ResolvedProgram:
    path: str
    #: First 16 hex chars of the file's SHA-256: a remembered grant is bound to these bytes.
    digest: str


_digest_cache: dict[tuple[str, int, int], str] = {}


def _file_digest(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _digest_cache.get(key)
    if cached is None:
        sha = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                sha.update(block)
        cached = sha.hexdigest()[:16]
        _digest_cache[key] = cached
    return cached


@dataclass(frozen=True, slots=True)
class ProgramResolver:
    """Resolves a bare program name in fixed trusted directories, nowhere else."""

    dirs: tuple[str, ...]

    @classmethod
    def with_extra(cls, extra: Iterable[str] = ()) -> ProgramResolver:
        return cls(dirs=(*default_trusted_dirs(), *(d for d in extra if d.strip())))

    def resolve(self, name: str) -> ResolvedProgram | None:
        if not _BARE_NAME.match(name):
            return None
        suffixes = (".exe", ".com") if sys.platform == "win32" else ("",)
        lower = name.lower()
        candidates = (
            [name]
            if sys.platform == "win32" and lower.endswith(suffixes)
            else [name + suffix for suffix in suffixes]
        )
        for folder in self.dirs:
            for candidate in candidates:
                path = Path(folder) / candidate
                try:
                    if path.is_file():
                        return ResolvedProgram(str(path), _file_digest(path))
                except OSError:
                    continue
        return None


def _program(token: str) -> str:
    """Bare program name, lower-cased, without the extension: `whoami.exe` -> `whoami`."""
    base = token.strip().lower()
    for ext in (".exe", ".com"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    return base


def _subcommand(tokens: Sequence[str]) -> str:
    for token in tokens[1:]:
        if not token.startswith("-"):
            return token.lower()
    return ""


def _is_curated(program: str, tokens: Sequence[str]) -> bool:
    if program not in CURATED_READONLY:
        return False
    allowed = CURATED_READONLY[program]
    if allowed is None:
        return True
    return len(tokens) == 1 and allowed == ()


def _starts_other_code(program: str, tokens: Sequence[str]) -> bool:
    """Whether the argv runs code its own arguments name (see the module docstring)."""
    if program in _INTERPRETERS or program in _LAUNCHERS:
        return True
    args = [token.lower() for token in tokens[1:]]
    if program == "find":
        return any(arg in _FIND_ACTIONS for arg in args)
    if program == "git":
        if any(arg.startswith(opt) for arg in args for opt in _GIT_EXEC_OPTIONS):
            return True
        return _subcommand(tokens) in _GIT_EXEC_SUBCOMMANDS
    return False


def _complex(
    key: str, argv: tuple[str, ...], *, needs_shell: bool, suffix: str = ""
) -> Classification:
    sha = hashlib.sha256((key + suffix).encode("utf-8")).hexdigest()[:16]
    return Classification(
        ShellClass.COMPLEX, "", f"complex:{sha}{suffix}", argv, needs_shell=needs_shell
    )


def _egress_suffix(egress: Sequence[str]) -> str:
    hosts = sorted({h.strip().lower() for h in egress if h.strip()})
    return f" egress={','.join(hosts)}" if hosts else ""


def classify(
    *,
    command: str | None = None,
    argv: list[str] | None = None,
    egress: Sequence[str] = (),
    resolver: ProgramResolver | None = None,
    readonly_allowed: bool = False,
) -> Classification:
    """Classify one shell request. Exactly one of `command`/`argv` is given.

    `readonly_allowed` is True only when the sandbox in use is the strong kind; the read-only
    exception never applies in the low-integrity fallback. A declared `egress` never is read-only
    and is always part of the target.
    """
    suffix = _egress_suffix(egress)
    if argv is None:
        # A free-form command line is never trusted enough to be read-only or remembered.
        line = command or ""
        return _complex(line, (line,), needs_shell=True, suffix=suffix)

    tokens = [str(t) for t in argv]
    joined = "\0".join(tokens)
    if not tokens:
        return Classification(ShellClass.COMPLEX, "", "complex:empty", (), needs_shell=False)
    # A program name with a control character, a path or a drive is not a bare trusted name.
    if _CONTROL_CHARS.search(tokens[0]) or not _BARE_NAME.match(tokens[0]):
        return _complex(joined, tuple(tokens), needs_shell=False, suffix=suffix)

    program = _program(tokens[0])
    if _starts_other_code(program, tokens):
        return _complex(joined, tuple(tokens), needs_shell=False, suffix=suffix)
    resolved = (resolver or ProgramResolver.with_extra()).resolve(tokens[0])
    if resolved is None:
        # Not in a trusted directory: it could not run in the sandbox anyway, and nothing about
        # it is known enough to remember.
        return _complex(joined, tuple(tokens), needs_shell=False, suffix=suffix)
    exec_argv = (resolved.path, *tokens[1:])
    if readonly_allowed and not suffix and _is_curated(program, tokens):
        return Classification(
            ShellClass.READONLY, program, f"readonly:{program}", exec_argv, needs_shell=False
        )
    subcommand = _subcommand(tokens)
    pattern = f"{program} {subcommand}".strip()
    key = f"{resolved.path}#{resolved.digest} {subcommand}".rstrip()
    return Classification(
        ShellClass.SIMPLE, pattern, f"cmd:{key}{suffix}", exec_argv, needs_shell=False
    )


class ShellInput(BaseModel):
    """Either a pre-split `argv`, or a single `command` line for the OS shell. Not both."""

    command: str | None = None
    argv: list[str] | None = None
    #: Hosts the command may reach, in `security.egress_allowlist` grammar. Default: no network.
    #: Shown in the confirmation (it is part of the target) and refused in OFFLINE/PRIVATE.
    egress: list[str] = []

    @model_validator(mode="after")
    def _one_of(self) -> ShellInput:
        if bool(self.command) == bool(self.argv):
            raise ValueError("give exactly one of 'command' or 'argv'")
        return self


SandboxFactory = Callable[[], Sandbox | None]
ShellPolicy = Callable[[], "ShellSettings"]


@dataclass(frozen=True, slots=True)
class ShellSettings:
    enabled: bool
    shell_program: tuple[str, ...]  # how to invoke the OS shell for a `command` line
    timeout_s: float = 60.0
    #: Extra folders a bare program name may be found in (`hands.trusted_program_dirs`).
    trusted_dirs: tuple[str, ...] = ()


def register_shell_tools(
    registry: ToolRegistry, sandbox_factory: SandboxFactory, settings: ShellPolicy
) -> None:
    """Register `shell.run`. The handler runs every command in the sandbox and never elsewhere."""

    def readonly_allowed() -> bool:
        sandbox = sandbox_factory()
        return sandbox is not None and sandbox.mode in _READONLY_MODES

    def classify_payload(payload: dict[str, Any]) -> Classification:
        return classify(
            command=payload.get("command"),
            argv=payload.get("argv"),
            egress=payload.get("egress") or (),
            resolver=ProgramResolver.with_extra(settings().trusted_dirs),
            readonly_allowed=readonly_allowed(),
        )

    async def run(payload: dict[str, Any]) -> dict[str, Any]:
        model = ShellInput.model_validate(payload)
        cfg = settings()
        if not cfg.enabled:
            return {"ok": False, "error": "shell.disabled", "detail": "shell.run is switched off"}
        sandbox = sandbox_factory()
        if sandbox is None:
            return {
                "ok": False,
                "error": "sandbox.unavailable",
                "detail": "no sandbox on this machine; shell is refused (fail closed)",
            }
        info = classify_payload(model.model_dump())
        exec_argv = [*cfg.shell_program, info.argv[0]] if info.needs_shell else list(info.argv)
        try:
            result: Result = await sandbox.run(
                exec_argv,
                egress=EgressDecl(hosts=tuple(model.egress)),
                limits=Limits(timeout_s=cfg.timeout_s),
            )
        except SandboxUnavailable as exc:
            return {"ok": False, "error": "sandbox.unavailable", "detail": str(exc)}
        return {
            "ok": result.ok,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }

    registry.register(
        ToolSpec(
            name="shell.run",
            description=(
                "Run a command sandboxed: argv (bare program name) or one shell line; egress = "
                "hosts it may reach."
            ),
            input_model=ShellInput,
            risk=Risk.HIGH,
            handler=run,
            side_effects=True,
            local=True,
            rememberable=True,
            targets=lambda payload: classify_payload(payload).target,
            network=lambda payload: bool(payload.get("egress")),
            lookup=lambda payload: " ".join(payload.get("argv") or [payload.get("command") or ""]),
        )
    )
