"""The git operations a proposal needs, and the ones it is never given.

Four verbs: read the current branch, start a new one, describe what changed, and go back. That is
the whole surface, and the absences are the point - there is no merge here, no push, no reset
--hard, no branch deletion and no tag. Nox writes a branch and stops; what happens to that branch is
a decision a person makes with their own git.

Every call names the repository explicitly (`-C`) rather than relying on a working directory, and
nothing here is ever handed a string a model wrote: the branch name is built from an identifier the
core generated, and the only free text is the commit message, which is passed as an argument rather
than through a shell. There is no shell at all - `create_subprocess_exec`, never `_shell`.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import NamedTuple

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "Diff",
    "GitError",
    "back_to",
    "branch_name",
    "commit_all",
    "current_branch",
    "diff_stat",
    "is_repository",
    "run_git",
    "start_branch",
]

#: What a proposal identifier may look like once it is part of a ref. Belt and braces: the id is
#: generated, but a ref is not a place to find out that an assumption was wrong.
SAFE_ID = re.compile(r"^[a-z0-9]{6,32}$")

GIT_TIMEOUT_S = 60.0


class GitError(Exception):
    """A git command that did not succeed. Carries what git said, which is usually the answer."""


async def run_git(repository: Path, *arguments: str, timeout_s: float = GIT_TIMEOUT_S) -> str:
    """One git command in `repository`. Returns stdout; raises `GitError` with stderr."""
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repository),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        process.kill()
        raise GitError(f"git {arguments[0]} took longer than {timeout_s:.0f}s") from exc
    if process.returncode != 0:
        message = err.decode("utf-8", errors="replace").strip() or f"exit {process.returncode}"
        raise GitError(f"git {arguments[0]}: {message}")
    return out.decode("utf-8", errors="replace")


async def is_repository(repository: Path) -> bool:
    try:
        return (await run_git(repository, "rev-parse", "--is-inside-work-tree")).strip() == "true"
    except (GitError, OSError):
        return False


async def current_branch(repository: Path) -> str:
    return (await run_git(repository, "rev-parse", "--abbrev-ref", "HEAD")).strip()


def branch_name(prefix: str, proposal_id: str) -> str:
    if not SAFE_ID.match(proposal_id):
        raise GitError(f"{proposal_id!r} is not a usable proposal id")
    return f"{prefix}{proposal_id}"


async def start_branch(repository: Path, prefix: str, proposal_id: str) -> str:
    """A new branch at the current HEAD, checked out. Never force, never over an existing one."""
    name = branch_name(prefix, proposal_id)
    await run_git(repository, "checkout", "-b", name)
    log.info("extend.branch_started", branch=name)
    return name


async def commit_all(repository: Path, message: str) -> bool:
    """Commit whatever the session wrote. False when it wrote nothing, which is not an error."""
    await run_git(repository, "add", "-A")
    staged = await run_git(repository, "diff", "--cached", "--name-only")
    if not staged.strip():
        return False
    await run_git(repository, "commit", "-m", message)
    return True


class Diff(NamedTuple):
    """What a branch changed, as data rather than a wall of text."""

    files: list[dict[str, str]]
    #: git's own one-line count: "3 files changed, 41 insertions(+), 2 deletions(-)".
    summary: str


async def diff_stat(repository: Path, against: str) -> Diff:
    """What this branch changed relative to where it started, as data rather than a wall of text."""
    names = await run_git(repository, "diff", "--name-status", f"{against}...HEAD")
    stat = await run_git(repository, "diff", "--shortstat", f"{against}...HEAD")
    files: list[dict[str, str]] = []
    for line in names.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append({"change": parts[0].strip(), "path": parts[-1].strip()})
    return Diff(files=files, summary=stat.strip())


async def back_to(repository: Path, branch: str) -> None:
    """Return to where we started. Used on every path out, including the failures."""
    try:
        await run_git(repository, "checkout", branch)
    except GitError as exc:
        # Leaving the user's checkout on a proposal branch would be a surprise with consequences,
        # so this is loud even though there is nothing left to do about it here.
        log.error("extend.checkout_not_restored", branch=branch, error=str(exc))
