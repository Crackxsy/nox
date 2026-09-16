"""nox.stream.sessions.StreamSessionService: session open/close, chat persistence + retention,
plugin/scene tracking, `stream.session.status` body."""

from __future__ import annotations

from datetime import UTC, datetime

from nox.core.config import StreamChatConfig
from nox.core.events import Event
from nox.data.stream_repos import ChatEventRepository, StreamSessionRepository, ViewerRepository
from nox.stream.sessions import StreamSessionService
from tests.unit.fakes import FakeBus


def make_service(
    bus: FakeBus,
    sessions: StreamSessionRepository,
    chat_events: ChatEventRepository,
    chat_config: StreamChatConfig,
    viewers: ViewerRepository | None = None,
) -> StreamSessionService:
    service = StreamSessionService(bus, sessions, chat_events, chat_config, viewers)
    service.start()
    return service


async def test_stream_started_opens_a_session_row(
    bus: FakeBus,
    stream_sessions_repo: StreamSessionRepository,
    chat_events: ChatEventRepository,
    chat_config: StreamChatConfig,
) -> None:
    service = make_service(bus, stream_sessions_repo, chat_events, chat_config)
    assert service.is_active() is False
    await bus.publish(Event(name="stream.started", payload={"session_id": "abc", "mode": "live"}))
    assert service.is_active() is True
    status = service.status()
    assert status["active"] is True
    assert status["session_id"] is not None
    assert status["started_at"] is not None
    row = stream_sessions_repo.active()
    assert row is not None and row.mode == "live"


async def test_stream_ended_closes_the_session_row(
    bus: FakeBus,
    stream_sessions_repo: StreamSessionRepository,
    chat_events: ChatEventRepository,
    chat_config: StreamChatConfig,
) -> None:
    service = make_service(bus, stream_sessions_repo, chat_events, chat_config)
    await bus.publish(Event(name="stream.started", payload={"session_id": "abc"}))
    session_id = service.active_session_id
    assert session_id is not None
    await bus.publish(
        Event(
            name="stream.ended",
            payload={"session_id": "abc", "duration_s": 120.0, "ended_reason": "manual"},
        )
    )
    assert service.is_active() is False
    assert service.status() == {
        "active": False,
        "session_id": None,
        "started_at": None,
        "scene": None,
        "plugins": {"obs": "unknown", "twitch": "unknown"},
    }
    row = stream_sessions_repo.get(session_id)
    assert row is not None and row.ended_at is not None and row.ended_reason == "manual"


async def test_chat_message_is_persisted_with_configured_retention(
    bus: FakeBus,
    stream_sessions_repo: StreamSessionRepository,
    chat_events: ChatEventRepository,
    chat_config: StreamChatConfig,
    viewers: ViewerRepository,
) -> None:
    service = make_service(bus, stream_sessions_repo, chat_events, chat_config, viewers)
    await bus.publish(Event(name="stream.started", payload={"session_id": "abc"}))
    session_id = service.active_session_id
    assert session_id is not None
    await bus.publish(
        Event(
            name="twitch.chat_message",
            payload={"chat_event_id": 0, "viewer_id": "v1", "text": "hello nox"},
        )
    )
    rows = chat_events.list_for_session(session_id)
    assert len(rows) == 1
    assert rows[0].text == "hello nox"
    assert rows[0].retain_until is not None
    row = stream_sessions_repo.get(session_id)
    assert row is not None and row.chat_message_count == 1


async def test_chat_message_is_metadata_only_when_retention_disabled(
    bus: FakeBus,
    stream_sessions_repo: StreamSessionRepository,
    chat_events: ChatEventRepository,
    viewers: ViewerRepository,
) -> None:
    service = make_service(
        bus, stream_sessions_repo, chat_events, StreamChatConfig(retain_raw_text_days=0), viewers
    )
    await bus.publish(Event(name="stream.started", payload={"session_id": "abc"}))
    session_id = service.active_session_id
    assert session_id is not None
    await bus.publish(
        Event(
            name="twitch.chat_message",
            payload={"chat_event_id": 0, "viewer_id": "v1", "text": "secret opinion"},
        )
    )
    rows = chat_events.list_for_session(session_id)
    assert len(rows) == 1
    assert rows[0].text == ""
    assert rows[0].retain_until is None


async def test_status_tracks_scene_and_plugin_connections(
    bus: FakeBus,
    stream_sessions_repo: StreamSessionRepository,
    chat_events: ChatEventRepository,
    chat_config: StreamChatConfig,
) -> None:
    service = make_service(bus, stream_sessions_repo, chat_events, chat_config)
    await bus.publish(Event(name="obs.connected", payload={}))
    await bus.publish(Event(name="twitch.connected", payload={}))
    await bus.publish(Event(name="obs.scene_changed", payload={"current_scene": "Just Chatting"}))
    status = service.status()
    assert status["plugins"] == {"obs": "connected", "twitch": "connected"}
    assert status["scene"] == "Just Chatting"
    await bus.publish(Event(name="obs.disconnected", payload={}))
    assert service.status()["plugins"]["obs"] == "disconnected"


async def test_resumes_an_already_open_session_on_start(
    stream_sessions_repo: StreamSessionRepository,
    chat_events: ChatEventRepository,
    chat_config: StreamChatConfig,
) -> None:
    stream_sessions_repo.start(mode="live", started_at=datetime(2026, 9, 13, 18, 0, tzinfo=UTC))
    service = StreamSessionService(FakeBus(), stream_sessions_repo, chat_events, chat_config)
    service.start()
    assert service.is_active() is True
    assert service.status()["active"] is True
