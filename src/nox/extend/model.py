"""What a proposal is: a branch, a diff, and whether the tests still pass.

A proposal is never "applied". It is a branch in a checkout the user named, with a commit on it and
a record of what the test suite said. Merging it, and restarting Nox on the result, are things a
person does with their own git - which is what keeps "Nox can extend itself" from meaning "Nox can
change what it is while you are not looking".
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Proposal", "ProposalState"]


class ProposalState(StrEnum):
    """How far a proposal got. Only `READY` is worth reading the diff of."""

    #: The coding session ran and the tests passed (or there were none, which is said out loud).
    READY = "ready"
    #: It changed something and the tests failed. The branch is there; the diff is evidence.
    TESTS_FAILED = "tests_failed"
    #: The session ran and changed nothing.
    EMPTY = "empty"
    #: Something went wrong before there was anything to read.
    FAILED = "failed"


class Proposal(BaseModel):
    """One attempt at changing Nox, as the dashboard and the model both read it."""

    model_config = ConfigDict(frozen=True)

    id: str
    #: What was asked for, in the user's words or the model's.
    intent: str = Field(min_length=1, max_length=2000)
    state: ProposalState
    branch: str = ""
    #: The branch it was started from, which is also where the checkout was put back.
    base: str = ""
    files: list[dict[str, str]] = Field(default_factory=list)
    summary: str = ""
    #: Empty when no test command is configured - which the note then says, rather than implying
    #: that silence means success.
    tests: str = ""
    note: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def readable(self) -> bool:
        """Is there a diff worth a person's time?"""
        return self.state in (ProposalState.READY, ProposalState.TESTS_FAILED)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
