"""`chat.history` against a real core: persisted turns across sessions, paged, and private.

What the dashboard shows when the Chat page opens: the turns the orchestrator recorded, oldest
first, including earlier sessions - and nothing that was said while memory writes were not
allowed, because nothing was recorded then.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nox.app import PROFILES_DIR, NoxCore
from nox.core.state import PrivacyMode
from nox.data.repos import TurnRepository
from nox.ipc.dispatch import RequestContext
from nox.ipc.handlers.core import ChatHistoryRequest, CoreHandlers
from nox.ipc.protocol import Envelope, Kind, Source
from tests.integration.test_walking_skeleton import _config


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[NoxCore]:
    instance = NoxCore(_config(tmp_path), voice=False, profiles_dir=PROFILES_DIR)
    await asyncio.wait_for(instance.start(), timeout=60)
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=30)


def _ctx() -> RequestContext:
    envelope = Envelope(
        kind=Kind.REQUEST, name="chat.history", src=Source(role="dashboard", id="dashboard:t")
    )
    return RequestContext(client_id="dashboard:t", role="dashboard", request=envelope)


async def _history(core: NoxCore, **payload: Any) -> dict[str, Any]:
    return await CoreHandlers(core).chat_history(_ctx(), ChatHistoryRequest(**payload))


def _earlier_session(core: NoxCore, *, expired: bool = False) -> None:
    """Two turns from a session before this boot, the way the orchestrator recorded them."""
    assert core.sessions is not None and core.db is not None
    core.sessions.create("companion", "balanced", session_id="earlier")
    turns = TurnRepository(core.db)
    past = datetime.now(UTC) - timedelta(days=1)
    retain = past if expired else past + timedelta(days=7)
    turns.add("earlier", "user", "Merk dir: Zahnarzt am Dienstag", ts=past, retain_until=retain)
    turns.add(
        "earlier", "assistant", "Ist notiert.", provider="rules", ts=past, retain_until=retain
    )


async def test_history_spans_sessions_and_pages_backwards(core: NoxCore) -> None:
    _earlier_session(core)
    assert core.orchestrator is not None
    await core.orchestrator.handle_text("wie spät ist es", speak=False)

    everything = await _history(core)
    texts = [turn["text"] for turn in everything["turns"]]
    assert texts[:2] == ["Merk dir: Zahnarzt am Dienstag", "Ist notiert."]
    assert texts[2] == "wie spät ist es"
    assert [turn["role"] for turn in everything["turns"][:3]] == ["user", "assistant", "user"]
    assert everything["turns"][1]["provider"] == "rules"
    assert everything["has_more"] is False
    assert len({turn["session_id"] for turn in everything["turns"]}) == 2

    newest = await _history(core, limit=2)
    assert [t["id"] for t in newest["turns"]] == [t["id"] for t in everything["turns"][-2:]]
    assert newest["has_more"] is True
    older = await _history(core, limit=2, before=newest["turns"][0]["id"])
    assert [t["id"] for t in older["turns"]] == [t["id"] for t in everything["turns"][:2]]


async def test_nothing_is_recorded_while_memory_writes_are_not_allowed(core: NoxCore) -> None:
    assert core.security is not None and core.orchestrator is not None
    await core.security.privacy.set_mode(PrivacyMode.PRIVATE, by="test")

    await core.orchestrator.handle_text("das bleibt unter uns", speak=False)

    assert (await _history(core))["turns"] == []


async def test_expired_turns_are_not_shown_even_before_they_are_purged(core: NoxCore) -> None:
    _earlier_session(core, expired=True)

    assert (await _history(core))["turns"] == []


def test_only_the_dashboard_may_read_the_history(core: NoxCore) -> None:
    assert core.registry is not None
    registration = core.registry.get("chat.history")
    assert registration is not None
    assert registration.allowed_roles == frozenset({"dashboard"})
