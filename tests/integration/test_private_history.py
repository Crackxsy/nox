"""What is said while cloud models are not allowed never reaches one later.

End to end on the real turn store (SQLite, migrations applied): a turn in privacy mode offline is
recorded and marked private; after the switch back, the next turn's request carries it marked,
and the payload a cloud provider builds from that request - the one-shot prompt and the warm
session's message alike - does not contain it. A local model still gets it.
"""

from __future__ import annotations

from pathlib import Path

from nox.ai.config import ClaudeCodeConfig
from nox.ai.providers.claude_code import render_prompt
from nox.ai.providers.claude_persistent import PersistentClaude
from nox.ai.providers.claude_session import ClaudeSession
from nox.core.boot.persistence import DbTurnStore
from nox.core.orchestrator import Orchestrator
from nox.data.db import Database
from nox.data.repos import SessionRepository, TurnRepository
from tests.unit.fakes import FakeBus, FakeRouter, FakeSpeaker, FakeState

SECRET = "Blaubeerkuchen-Rezept der Nachbarin"


class Policy:
    def allows_memory_write(self) -> bool:
        return True  # offline still records; private mode would not record at all


async def test_an_offline_turn_never_reaches_a_cloud_provider_later(tmp_path: Path) -> None:
    db = Database(tmp_path / "nox.db")
    db.migrate()
    session_id = "s1"
    SessionRepository(db).create("companion", "offline", session_id=session_id)
    cloud = [False]
    bus = FakeBus()
    router = FakeRouter(bus, text="Ok.")
    orchestrator = Orchestrator(
        bus=bus,
        state=FakeState(),
        router=router,
        speaker=FakeSpeaker(),
        turns=DbTurnStore(TurnRepository(db), None),
        memory_policy=Policy(),
        system_prompt=lambda: "You are Nox.",
        session_id=session_id,
        cloud_allowed=lambda: cloud[0],
    )

    await orchestrator.handle_text(f"Merk dir: {SECRET}", speak=False)
    cloud[0] = True
    await orchestrator.handle_text("Was gibt es Neues?", speak=False)

    request = router.requests[-1]
    marked = [m for m in request.messages if SECRET in m.content]
    assert marked and all(m.private for m in marked), "the offline turn is in history, marked"

    _system, one_shot_prompt = render_prompt(request)
    assert SECRET not in one_shot_prompt

    provider = PersistentClaude(
        ClaudeCodeConfig(), executable=list, creationflags=0, workdir=tmp_path / "c", env={}
    )
    session = ClaudeSession(provider.spec("", "balanced", None))
    turns = [m for m in request.messages if m.role in ("user", "assistant")]
    session_payload = provider._compose(session, request, turns)  # noqa: SLF001 - what it sends
    assert SECRET not in session_payload
    assert "Was gibt es Neues?" in session_payload
    db.close()
