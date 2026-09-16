"""`coding.review.request`: a short, honest triage of a tracked session's outcome - what is broken,
why probably, and the next action. Built only from the session record's own state (outcome, error,
repair attempts); a session with no diagnostic yet says so rather than fabricating one
(ENGINEERING.md "no fake implementations")."""

from __future__ import annotations

from dataclasses import dataclass

from .session import SessionOutcome, SessionRecord


@dataclass(frozen=True, slots=True)
class Review:
    what_is_broken: str
    why_probably: str
    next_action: str

    def as_text(self) -> str:
        return (
            f"What is broken: {self.what_is_broken}\n"
            f"Why (probably): {self.why_probably}\n"
            f"Next action: {self.next_action}"
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "what_is_broken": self.what_is_broken,
            "why_probably": self.why_probably,
            "next_action": self.next_action,
        }


def format_review(record: SessionRecord | None) -> Review:
    if record is None:
        return Review(
            what_is_broken="unknown - no session found",
            why_probably="the session id does not match any tracked session",
            next_action="check the session id, e.g. via coding.session.status.read",
        )
    if record.outcome is SessionOutcome.RUNNING:
        return Review(
            what_is_broken="nothing yet - the session is still running",
            why_probably="n/a",
            next_action="wait for coding.session_ended/coding.session_failed, "
            "or call coding.session.stop to cancel it",
        )
    if record.outcome is SessionOutcome.ENDED and not record.last_error:
        return Review(
            what_is_broken="nothing - the session finished without an error",
            why_probably="n/a",
            next_action="review the diff and tests on the dashboard before merge",
        )
    if record.outcome is SessionOutcome.CANCELLED:
        return Review(
            what_is_broken="the session was stopped before finishing",
            why_probably=record.last_error or "stopped on request",
            next_action="resume with coding.session.start against the same workspace, "
            "or check what was left uncommitted",
        )
    reason = (record.last_error or "unspecified failure").strip()
    lowered = reason.lower()
    if "max turns" in lowered:
        return Review(
            what_is_broken="the session ran out of turns before finishing",
            why_probably=reason,
            next_action="start a fresh session with a narrower prompt, or raise max_turns",
        )
    if "logged in" in lowered or "login" in lowered or "authentication" in lowered:
        return Review(
            what_is_broken="the Claude Code CLI is not logged in",
            why_probably=reason,
            next_action="log in the CLI (`claude /login`) and retry",
        )
    if record.repair_attempts >= 1:
        return Review(
            what_is_broken=f"{record.repair_attempts} repair attempt(s) did not fix it: {reason}",
            why_probably="the underlying issue needs a person to look at it - Nox stopped after "
            "the repair-attempt limit per policy (Personality v1 B.8: honesty over infinite retry)",
            next_action="read the full session detail on the dashboard and either fix it manually "
            "or start a new session with more context",
        )
    return Review(
        what_is_broken=reason,
        why_probably="not enough information to say more",
        next_action="check coding.session.status.read for the raw error and retry",
    )
