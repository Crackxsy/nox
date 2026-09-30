"""Proposing a change to Nox, against a real git repository.

A temporary repo rather than a mocked one, because the property worth testing is the one a mock
would have granted for free: that the user's checkout is put back on *every* path out. A proposal
that fails halfway and leaves the repository sitting on a branch nobody made is the failure this
design exists to avoid, and it is invisible until it happens to someone.

The coding session itself is a stand-in. It is the Claude Code CLI; running it in a unit test would
be testing Anthropic's product, slowly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from nox.core.config.extend import ExtendConfig
from nox.extend.model import ProposalState
from nox.extend.runner import CODING_TOOL, ProposalRunner


class Outcome:
    def __init__(self, ok: bool = True, error: str | None = None) -> None:
        self.ok = ok
        self.data: dict[str, Any] | None = {}
        self.error = error


def git(where: Path, *arguments: str) -> str:
    done = subprocess.run(["git", "-C", str(where), *arguments], capture_output=True, check=True)
    return done.stdout.decode("utf-8", errors="replace")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A repository with one commit, on a branch with a name worth putting back."""
    place = tmp_path / "checkout"
    place.mkdir()
    git(place, "init", "-b", "develop")
    git(place, "config", "user.email", "test@example.invalid")
    git(place, "config", "user.name", "Test")
    (place / "README.md").write_text("start\n", encoding="utf-8")
    git(place, "add", "-A")
    git(place, "commit", "-m", "first")
    return place


def config(workspace: Path | None = None, **over: Any) -> ExtendConfig:
    payload: dict[str, Any] = {"workspace": str(workspace) if workspace else ""}
    payload.update(over)
    return ExtendConfig.model_validate(payload)


def runner(
    settings: ExtendConfig,
    *,
    writes: str | None = "changed\n",
    ok: bool = True,
    known: bool = True,
) -> tuple[ProposalRunner, list[str]]:
    """A runner whose "coding session" writes a file, or does not, or fails."""
    called: list[str] = []
    place = Path(settings.workspace) if settings.workspace else None

    async def call(name: str, arguments: dict[str, Any]) -> Outcome:
        called.append(name)
        if writes is not None and place is not None:
            (place / "README.md").write_text(writes, encoding="utf-8")
        return Outcome(ok=ok, error=None if ok else "the session fell over")

    return ProposalRunner(lambda: settings, call, lambda _n: known), called


# ---- the checkout comes back ---------------------------------------------------------------


async def test_a_proposal_leaves_the_checkout_where_it_was(workspace: Path) -> None:
    """The property the whole design rests on."""
    engine, _ = runner(config(workspace))

    await engine.propose("write something into the readme")

    assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip() == "develop"


async def test_the_checkout_comes_back_even_when_the_session_fails(workspace: Path) -> None:
    engine, _ = runner(config(workspace), ok=False)

    proposal = await engine.propose("write something into the readme")

    assert proposal.state is ProposalState.FAILED
    assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip() == "develop"


async def test_the_work_is_on_a_branch_and_not_on_the_users_own(workspace: Path) -> None:
    engine, _ = runner(config(workspace))

    proposal = await engine.propose("write something into the readme")

    assert proposal.branch.startswith("nox/proposal/")
    # The branch has the change; `develop` does not. Nothing was merged.
    assert "changed" in git(workspace, "show", f"{proposal.branch}:README.md")
    assert "start" in (workspace / "README.md").read_text(encoding="utf-8")


# ---- what it reports -----------------------------------------------------------------------


async def test_a_change_is_reported_as_a_diff(workspace: Path) -> None:
    engine, called = runner(config(workspace))

    proposal = await engine.propose("write something into the readme")

    assert proposal.state is ProposalState.READY
    assert [entry["path"] for entry in proposal.files] == ["README.md"]
    assert "1 file changed" in proposal.summary
    assert called == [CODING_TOOL]


async def test_a_session_that_changed_nothing_says_so(workspace: Path) -> None:
    """Not a failure and not a success: there is no diff to read, and that is the answer."""
    engine, _ = runner(config(workspace), writes=None)

    proposal = await engine.propose("think about the readme")

    assert proposal.state is ProposalState.EMPTY
    assert "changed nothing" in proposal.note


async def test_without_a_test_command_it_does_not_claim_the_tests_passed(
    workspace: Path,
) -> None:
    engine, _ = runner(config(workspace))

    proposal = await engine.propose("write something into the readme")

    assert "no test command is configured" in proposal.tests


async def test_a_failing_suite_is_reported_with_the_diff_kept(workspace: Path) -> None:
    """The branch stays: a diff that fails its tests is still the evidence of what went wrong."""
    import sys

    settings = config(
        workspace,
        test_command=[sys.executable, "-c", "import sys; print('boom'); sys.exit(1)"],
    )
    engine, _ = runner(settings)

    proposal = await engine.propose("write something into the readme")

    assert proposal.state is ProposalState.TESTS_FAILED
    assert "boom" in proposal.tests
    assert proposal.files, "the diff has to survive a red suite"


# ---- the reasons it will not start ----------------------------------------------------------


async def test_no_workspace_means_no_proposal(tmp_path: Path) -> None:
    engine, called = runner(config())

    proposal = await engine.propose("change something")

    assert proposal.state is ProposalState.FAILED
    assert "extend.workspace" in proposal.note
    assert called == [], "nothing may be asked for before there is somewhere to put it"


async def test_a_folder_that_is_not_a_repository_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    engine, called = runner(config(plain))

    proposal = await engine.propose("change something")

    assert "not a git repository" in proposal.note
    assert called == []


async def test_without_the_coding_plugin_it_says_which_profile_to_use(workspace: Path) -> None:
    engine, called = runner(config(workspace), known=False)

    proposal = await engine.propose("change something")

    assert "coding profile" in proposal.note
    assert called == []


async def test_switched_off_means_off(workspace: Path) -> None:
    engine, called = runner(config(workspace, enabled=False))

    proposal = await engine.propose("change something")

    assert "switched off" in proposal.note
    assert called == []


# ---- the record ------------------------------------------------------------------------------


async def test_the_newest_proposal_is_first_and_the_list_is_bounded(workspace: Path) -> None:
    engine, _ = runner(config(workspace, keep=2))

    for index in range(3):
        await engine.propose(f"change number {index}")

    kept = engine.all()
    assert len(kept) == 2
    assert kept[0].intent == "change number 2"
    assert engine.get(kept[0].id) is kept[0]
