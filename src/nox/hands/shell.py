"""`shell.run`: run a command in the sandbox, with the command-pattern grant the spec asks for.

Two input shapes (both from the spec's "argv oder Befehlszeile"):

* **argv** - a list already split into program and arguments. It is run *without a shell*, so none
  of `;`, `&&`, `|`, `>` means anything; it is as safe as a command gets and classifies by its
  program and subcommand.
* **command** - one line for the OS shell (`pwsh -Command` / `bash -c`). The shell interprets
  operators, so a line containing a shell metacharacter (``; && || | > < & `` backtick ``$( ( {``),
  an ``ENV=val`` prefix, or an interpreter called with ``-c``/``-Command``, is **complex**: it
  always asks and can never match a remembered pattern (Security Model §1).

Classification decides the permission target, which is how the engine tells the three cases apart
without parsing commands itself:

* `readonly:<pattern>` - a vetted read-only command (`CURATED_READONLY`); the engine allows it in
  the sandbox with no confirmation (confirmation fatigue is a UX bug).
* `cmd:<pattern>` - a simple, non-curated command; HIGH, CONFIRM, and "remember" stores exactly
  this pattern as a `cmd:` grant for the session.
* `complex:<sha>` - anything with a shell operator etc.; HIGH, CONFIRM, never remembered.

The pattern is `argv0 + first subcommand token` (`git status`, `winget`), never the arguments, so a
remembered `git status` never covers `git push`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, model_validator

from nox.core.logging import get_logger
from nox.sandbox import EgressDecl, Limits, Result, Sandbox, SandboxUnavailable
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = ["CURATED_READONLY", "ShellClass", "classify", "register_shell_tools"]

#: Any ASCII control character or DEL. An argv0 that carries one is not a real program name; a
#: free-form command line is already always complex, so this guards the argv path.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
#: An interpreter asked to run inline code; the subcommand token tells nothing about what it does.
_INTERPRETERS = frozenset(
    {
        "python",
        "python3",
        "py",
        "node",
        "deno",
        "bun",
        "bash",
        "sh",
        "zsh",
        "pwsh",
        "powershell",
        "cmd",
        "perl",
        "ruby",
        "php",
    }
)


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
    #: `argv0 subcommand`, the unit a `cmd:`/`readonly:` grant is keyed on.
    pattern: str
    #: The permission target the engine sees (`readonly:`/`cmd:`/`complex:` prefixed).
    target: str
    argv: tuple[str, ...]
    #: True when the line must go through an OS shell (the `command` form), not argv directly.
    needs_shell: bool


#: Vetted read-only commands: {program: allowed subcommands, or None for "any arguments"}.
#: Only commands that observe and never change the machine. A program with a subcommand set
#: (git) matches only those subcommands; anything else falls back to a confirmed simple command.
CURATED_READONLY: dict[str, frozenset[str] | None] = {
    "df": None,
    "free": None,
    "uptime": None,
    "uname": None,
    "hostname": None,
    "whoami": None,
    "ls": None,
    "dir": None,
    "pwd": None,
    "ping": None,
    "git": frozenset({"status", "log", "diff", "branch", "remote", "show", "config"}),
    "get-process": None,
    "get-psdrive": None,
    "get-childitem": None,
    "get-computerinfo": None,
    "get-ciminstance": None,
}


def _program(token: str) -> str:
    """Bare program name, lower-cased, without a path or extension: `C:\\...\\git.exe` -> `git`."""
    base = re.split(r"[\\/]", token.strip())[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    return base


def _pattern(program: str, tokens: list[str]) -> str:
    subcommand = ""
    for token in tokens[1:]:
        if not token.startswith("-"):
            subcommand = token.lower()
            break
    return f"{program} {subcommand}".strip()


def _is_curated(program: str, tokens: list[str]) -> bool:
    if program not in CURATED_READONLY:
        return False
    allowed = CURATED_READONLY[program]
    if allowed is None:
        return True
    for token in tokens[1:]:
        if not token.startswith("-"):
            return token.lower() in allowed
    # A bare `git` with no subcommand prints help: harmless, treat as read-only.
    return True


def _inline_interpreter(program: str, tokens: list[str]) -> bool:
    if program not in _INTERPRETERS:
        return False
    return any(_is_inline_flag(token) for token in tokens[1:])


def _complex(key: str) -> Classification:
    sha = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Classification(ShellClass.COMPLEX, "", f"complex:{sha}", (key,), needs_shell=True)


def classify(*, command: str | None = None, argv: list[str] | None = None) -> Classification:
    """Classify one shell request. Exactly one of `command`/`argv` is given.

    The read-only auto-allow and the rememberable `cmd:` pattern apply **only to the argv form**,
    which is run without a shell and so has no operator, quoting or control-character ambiguity. A
    free-form `command` line is *always* COMPLEX: it goes through the OS shell, it always asks, and
    it is never remembered. That is the safe reading of a request Nox cannot fully parse - a
    newline, a `$(...)`, an `ENV=` prefix or an interpreter `-c` can all turn one apparent command
    into another.
    """
    if argv is None:
        # A free-form command line is never trusted enough to be read-only or remembered.
        return _complex(command or "")

    tokens = [str(t) for t in argv]
    if not tokens:
        return Classification(ShellClass.COMPLEX, "", "complex:empty", (), needs_shell=False)
    # Even in argv form, a program name carrying a control character is not a real program name.
    if _CONTROL_CHARS.search(tokens[0]):
        return _complex(" ".join(tokens))

    program = _program(tokens[0])
    pattern = _pattern(program, tokens)
    if _inline_interpreter(program, tokens):
        return Classification(
            ShellClass.COMPLEX,
            "",
            f"complex:{hashlib.sha256(' '.join(tokens).encode()).hexdigest()[:16]}",
            tuple(tokens),
            needs_shell=False,
        )
    if _is_curated(program, tokens):
        return Classification(
            ShellClass.READONLY, pattern, f"readonly:{pattern}", tuple(tokens), needs_shell=False
        )
    return Classification(
        ShellClass.SIMPLE, pattern, f"cmd:{pattern}", tuple(tokens), needs_shell=False
    )


class ShellInput(BaseModel):
    """Either a pre-split `argv`, or a single `command` line for the OS shell. Not both."""

    command: str | None = None
    argv: list[str] | None = None
    #: Hosts the command may reach, in `security.egress_allowlist` grammar. Default: no network.
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


def register_shell_tools(
    registry: ToolRegistry, sandbox_factory: SandboxFactory, settings: ShellPolicy
) -> None:
    """Register `shell.run`. The handler runs every command in the sandbox and never elsewhere."""

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
        info = classify(command=model.command, argv=model.argv)
        if info.needs_shell:
            exec_argv = [*cfg.shell_program, info.argv[0]] if info.argv else list(cfg.shell_program)
        else:
            exec_argv = list(info.argv)
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
                "Run a command in the sandbox. Give 'argv' (a pre-split list, run without a shell) "
                "or 'command' (one line for the OS shell). Curated read-only commands (df, ls/dir, "
                "git status, Get-Process, uname, free, ...) run without a confirmation; everything "
                "else asks first and can be remembered per command pattern for the session."
            ),
            input_model=ShellInput,
            risk=Risk.HIGH,
            handler=run,
            side_effects=True,
            local=True,
            rememberable=True,
            targets=lambda payload: (
                classify(command=payload.get("command"), argv=payload.get("argv")).target
            ),
        )
    )
