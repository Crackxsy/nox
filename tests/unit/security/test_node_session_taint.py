"""While Nox controls a paired PC, a message from a paired device may have been typed by Nox on
that PC. Its conversation counts as holding unreadable untrusted content, so a lookup in it is
never "user-directed": a URL taken from that very message still asks before it is fetched.
Real permission engine and taint ledger; the conversation itself is a fake."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.handlers.core import ChatSend, CoreHandlers
from nox.ipc.protocol import Envelope, Kind, Source
from nox.security.model import Decision, PermissionRequest, Risk
from nox.security.permissions import DefaultPermissionEngine

PHONE = "device:" + "e" * 32
MESSAGE = "Lies https://news.example.org/artikel/2 und such nach Springfield"


def _fetch(url: str, conversation: str) -> PermissionRequest:
    return PermissionRequest(
        agent="companion",
        tool="web",
        action="fetch",
        mode="companion",
        risk=Risk.LOW,
        target=url,
        network=True,
        conversation=conversation,
        lookup=url,
    )


def _search(query: str, conversation: str) -> PermissionRequest:
    return PermissionRequest(
        agent="companion",
        tool="web",
        action="search",
        mode="companion",
        risk=Risk.LOW,
        target=query,
        network=True,
        conversation=conversation,
        lookup=query,
    )


async def test_a_paired_devices_message_during_a_session_never_directs_a_lookup(
    engine: DefaultPermissionEngine,
) -> None:
    class Orchestrator:
        def conversation_for(self, channel: str) -> str:
            return f"{channel}#1"

        async def handle_text(self, text: str, **kw: Any) -> Any:
            # What the tool loop does with every turn: the user's words are noted.
            engine.note_user_message(f"{kw['channel']}#1", text)
            return SimpleNamespace(
                request_id="r", response="ok", provider="x", degraded=False, tools_used=[]
            )

    session = {"active": True}

    class Session:
        """A condition that taints a paired device's words while a session runs."""

        name, precedence, wire_reason, reads_pin = "session", 20, "node_session", True

        def chat_untrusted(self, client: Any) -> bool:
            return bool(client.role == "device" and session["active"])

    registry = RequestRegistry()
    registry.policy.add_condition(Session())  # type: ignore[arg-type]
    core = SimpleNamespace(
        orchestrator=Orchestrator(),
        registry=registry,
        security=SimpleNamespace(engine=engine),
    )
    handlers = CoreHandlers(core)  # type: ignore[arg-type]

    async def stream(_payload: Any, _done: bool) -> None:
        return None

    phone = RequestContext(
        client_id=PHONE,
        role="device",
        request=Envelope(kind=Kind.REQUEST, name="chat.send", src=Source(role="device", id=PHONE)),
        stream=stream,
    )
    await handlers.chat_send(phone, ChatSend(text=MESSAGE))  # type: ignore[arg-type]
    conversation = f"chat:{PHONE}#1"
    # The URL and the search are word for word in the message - and still asked.
    assert engine.check(_fetch("https://news.example.org/artikel/2", conversation)).decision is (
        Decision.CONFIRM
    )
    assert engine.check(_search("Springfield", conversation)).decision is Decision.CONFIRM
    assert engine.check(_fetch("https://collect.example.invalid/c?d=x", conversation)).decision is (
        Decision.CONFIRM
    )


def test_marking_the_text_alone_would_not_have_been_enough(engine: DefaultPermissionEngine) -> None:
    """Why the conversation is marked unreadable, not with the message's text: the user's own
    words (noted by the tool loop) would make the same URL user-directed again."""
    conversation = "chat:device:x#1"
    engine.mark_tainted(conversation, MESSAGE)
    engine.note_user_message(conversation, MESSAGE)
    fetch = _fetch("https://news.example.org/artikel/2", conversation)
    assert engine.check(fetch).decision is Decision.ALLOW
    engine.mark_tainted(conversation, None)
    assert engine.check(fetch).decision is Decision.CONFIRM
