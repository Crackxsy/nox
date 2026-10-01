"""Running one proposal: branch, session, tests, diff - and always back where we started.

The shape is a `try/finally` around somebody else's checkout, and the `finally` is the part that
matters. A proposal that fails halfway must not leave the user's repository sitting on a branch they
did not make: the next thing they do there would be on top of a machine's half-finished idea.

The change itself is written by the `coding` plugin, which already is the right tool for it - a
Claude Code session with Read, Edit, Write, Glob and Grep and deliberately no shell, scoped to a
workspace under its own `filesystem_roots`. This module does not write code. It prepares a place for
one to be written, asks, and reports what came back.

Nothing here merges, pushes or restarts anything.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from nox.core.config.extend import ExtendConfig
from nox.core.logging import get_logger
from nox.extend import repo
from nox.extend.model import Proposal, ProposalState

log = get_logger(__name__)

__all__ = ["CODING_TOOL", "ProposalRunner"]

#: The plugin tool that actually writes the change. Absent unless the `coding` plugin is running,
#: which it only does under the `coding` profile - a state the answer names rather than hides.
CODING_TOOL = "coding.session.start"


class Outcome(Protocol):
    ok: bool
    data: dict[str, Any] | None
    error: str | None


CallTool = Callable[[str, dict[str, Any]], Awaitable[Outcome]]
Known = Callable[[str], bool]


def _commit_message(intent: str, *, unfinished: bool = False) -> str:
    first = intent.strip().splitlines()[0][:72]
    return f"proposal (unfinished): {first}" if unfinished else f"proposal: {first}"


#: For what the test suite leaves in the checkout after a finished proposal: caches, reports.
LEFT_BY_TESTS = "proposal: files the test run left behind"

#: Colour and cursor sequences a test runner writes; the result is read in a dashboard, not a TTY.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _session_failure(outcome: Outcome) -> str:
    """Why the coding session did not finish, or an empty string when it did.

    The call succeeding is not the session succeeding: `coding.session.start` answers with the
    session's own outcome, and a session that crashed or was cancelled is not one that "changed
    nothing".
    """
    if not outcome.ok:
        return str(outcome.error or "the coding session did not run")
    data = outcome.data or {}
    result = str(data.get("outcome", ""))
    if result == "ended":
        return ""
    detail = str(data.get("last_error") or "").strip()
    reason = f"the coding session ended as {result or 'unknown'}"
    return f"{reason}: {detail}" if detail else reason


class ProposalRunner:
    """Turns an intent into a branch someone can read, or into a sentence saying why not."""

    def __init__(
        self,
        settings: Callable[[], ExtendConfig],
        call_tool: CallTool,
        tool_known: Known,
    ) -> None:
        self._settings = settings
        self._call = call_tool
        self._known = tool_known
        self._proposals: list[Proposal] = []

    # ---- what happened -------------------------------------------------------------------------

    def all(self) -> list[Proposal]:
        return list(self._proposals)

    def get(self, proposal_id: str) -> Proposal | None:
        return next((p for p in self._proposals if p.id == proposal_id), None)

    def _record(self, proposal: Proposal) -> Proposal:
        self._proposals.insert(0, proposal)
        del self._proposals[self._settings().keep :]
        log.info("extend.proposal", id=proposal.id, state=proposal.state.value)
        return proposal

    async def _why_not(self, config: ExtendConfig) -> str:
        """The reason a proposal cannot even be started, or an empty string."""
        if not config.enabled:
            return "self-extension is switched off (extend.enabled)"
        if not config.workspace.strip():
            return (
                "no checkout is configured for this; name one in extend.workspace and Nox will "
                "propose changes on a branch there"
            )
        workspace = Path(config.workspace)
        if not await asyncio.to_thread(workspace.is_dir):
            return f"{workspace} is not there"
        if not await repo.is_repository(workspace):
            return f"{workspace} is not a git repository, so there is nowhere to put a branch"
        if not self._known(CODING_TOOL):
            return (
                "the coding plugin is not running, and it is what writes the change. It only runs "
                "under the coding profile - switch to it and ask again"
            )
        return ""

    # ---- the one thing it does -----------------------------------------------------------------

    async def propose(self, intent: str) -> Proposal:
        """Ask for a change, and come back with a branch or with the reason there is none."""
        config = self._settings()
        proposal_id = uuid4().hex[:12]
        refuse = await self._why_not(config)
        if refuse:
            return self._record(
                Proposal(id=proposal_id, intent=intent, state=ProposalState.FAILED, note=refuse)
            )

        workspace = Path(config.workspace)
        base = await repo.current_branch(workspace)
        branch = ""
        finished = False
        try:
            branch = await repo.start_branch(workspace, config.branch_prefix, proposal_id)
            proposal = await self._work(proposal_id, intent, config, workspace, base, branch)
            finished = proposal.state is not ProposalState.FAILED
            return self._record(proposal)
        except repo.GitError as exc:
            return self._record(
                Proposal(
                    id=proposal_id,
                    intent=intent,
                    state=ProposalState.FAILED,
                    base=base,
                    branch=branch,
                    note=str(exc),
                )
            )
        finally:
            # Every path out, including the ones that raised. A checkout left on a machine's
            # half-finished branch is the surprise this whole design exists to avoid - and so is
            # its half-finished work: git carries uncommitted changes across a checkout, so
            # whatever is left is committed on the proposal's branch first: a session's unfinished
            # work, or what the test run of a finished one wrote, each under its own name.
            if branch:
                message = LEFT_BY_TESTS if finished else _commit_message(intent, unfinished=True)
                await self._keep_on_branch(workspace, message)
            await repo.back_to(workspace, base)

    async def _keep_on_branch(self, workspace: Path, message: str) -> None:
        try:
            if await repo.commit_all(workspace, message):
                log.warning("extend.leftovers_kept_on_branch", commit=message)
        except repo.GitError as exc:
            log.error("extend.leftovers_not_kept", error=str(exc))

    async def _work(
        self,
        proposal_id: str,
        intent: str,
        config: ExtendConfig,
        workspace: Path,
        base: str,
        branch: str,
    ) -> Proposal:
        outcome = await self._call(CODING_TOOL, {"target": str(workspace), "prompt": intent})
        failure = _session_failure(outcome)
        if failure:
            return Proposal(
                id=proposal_id,
                intent=intent,
                state=ProposalState.FAILED,
                base=base,
                branch=branch,
                note=f"{failure}; anything it wrote is kept on {branch}, unfinished",
            )

        if not await repo.commit_all(workspace, _commit_message(intent)):
            said = str((outcome.data or {}).get("summary") or "").strip()
            return Proposal(
                id=proposal_id,
                intent=intent,
                state=ProposalState.EMPTY,
                base=base,
                branch=branch,
                note=f"the session ran and changed nothing; it said: {said}"
                if said
                else "the session ran and changed nothing",
            )

        diff = await repo.diff_stat(workspace, base)
        tests, passed = await self._tests(config, workspace)
        return Proposal(
            id=proposal_id,
            intent=intent,
            state=ProposalState.READY if passed else ProposalState.TESTS_FAILED,
            base=base,
            branch=branch,
            files=diff.files,
            summary=diff.summary,
            tests=tests,
            note=f"on branch {branch}; nothing was merged",
        )

    async def _tests(self, config: ExtendConfig, workspace: Path) -> tuple[str, bool]:
        """What the suite said, and whether it passed. No command configured is not a pass."""
        if not config.test_command:
            return ("no test command is configured (extend.test_command), so nothing was run", True)
        process = await asyncio.create_subprocess_exec(
            *config.test_command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout=config.test_timeout_s)
        except TimeoutError:
            process.kill()
            return (f"the tests took longer than {config.test_timeout_s:.0f}s", False)
        text = _ANSI.sub("", out.decode("utf-8", errors="replace"))
        # The tail is where a test runner says what failed; the head is machine configuration.
        tail = "\n".join(text.strip().splitlines()[-20:])
        return (tail, process.returncode == 0)
