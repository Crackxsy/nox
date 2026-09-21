"""RequestRegistry: glob matching, role allow-lists, validation/not_found/permission/internal."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from nox.ipc.dispatch import (
    ROLE_ALLOWLIST,
    EmptyPayload,
    RequestContext,
    RequestRegistry,
    match_name,
    role_allows,
    service_patterns,
)
from nox.ipc.errors import (
    CORE_SOURCE,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_PERMISSION,
    ERR_VALIDATION,
    IpcError,
)
from nox.ipc.protocol import NAME_ERROR, Envelope, Kind, Source

from .conftest import ModeSet, build_registry


@pytest.mark.parametrize(
    ("pattern", "name", "expected"),
    [
        ("voice.*", "voice.muted", True),
        ("voice.*", "voice.transcript.ready", False),
        ("voice.**", "voice.transcript.ready", True),
        ("**", "a.b.c", True),
        ("**", "a", True),
        ("security.*", "security.permission.reply", False),
        ("security.**", "security.permission.reply", True),
        ("voice.transcript_*", "voice.transcript_ready", True),
        ("voice.transcript_*", "voice.muted", False),
        ("state.get", "state.get", True),
        ("state.get", "state.set", False),
        ("*.get", "state.get", True),
    ],
)
def test_match_name(pattern: str, name: str, expected: bool) -> None:
    assert match_name(pattern, name) is expected


@pytest.mark.parametrize(
    ("role", "name", "expected"),
    [
        ("pet", "pet.interact", True),
        ("pet", "state.get", True),
        ("pet", "ipc.ping", True),
        ("pet", "mode.set", False),
        ("pet", "chat.send", False),
        ("dashboard", "state.get", True),
        ("dashboard", "mode.set", True),
        ("dashboard", "security.kill", True),
        ("dashboard", "security.permission.reply", False),
        ("dashboard", "voice.ptt", False),
        ("dashboard", "chat.send", True),
        ("shell", "voice.ptt", True),
        ("shell", "security.permission.reply", True),
        ("shell", "chat.send", True),
        ("worker", "worker.register", True),
        ("worker", "state.get", False),
        ("plugin", "mode.set", False),
        ("core", "state.get", False),
        ("remote", "state.get", False),
    ],
)
def test_role_allowlist(role: str, name: str, expected: bool) -> None:
    assert role_allows(role, name) is expected


def test_worker_service_namespace_grants() -> None:
    assert not role_allows("worker", "stt.transcribe")
    assert role_allows("worker", "stt.transcribe", service_patterns(["stt"]))
    assert not role_allows("worker", "tts.speak", service_patterns(["stt"]))


def test_allowlist_covers_every_role() -> None:
    assert set(ROLE_ALLOWLIST) == set(Source.model_fields["role"].annotation.__args__)  # type: ignore[union-attr]


def _req(name: str, payload: dict[str, Any], role: str = "shell") -> Envelope:
    return Envelope(
        kind=Kind.REQUEST, name=name, src=Source(role=role, id=f"{role}:1"), payload=payload
    )  # type: ignore[arg-type]


def _ctx(env: Envelope) -> RequestContext:
    return RequestContext(client_id=env.src.id, role=env.src.role, request=env)


async def test_dispatch_round_trip() -> None:
    reg = build_registry({})
    env = _req("state.get", {"path": "assistant.mood"})
    reply = await reg.dispatch(_ctx(env), env)
    assert reply.kind is Kind.RESPONSE
    assert reply.corr == env.id
    assert reply.name == "state.get"
    assert reply.src == CORE_SOURCE
    assert reply.payload["path"] == "assistant.mood"
    assert reply.payload["role"] == "shell"


async def test_dispatch_unknown_name() -> None:
    reg = build_registry({})
    env = _req("nope.nothing", {})
    reply = await reg.dispatch(_ctx(env), env)
    assert reply.kind is Kind.ERROR and reply.name == NAME_ERROR
    assert reply.payload["code"] == ERR_NOT_FOUND


async def test_dispatch_permission_denied_by_role() -> None:
    reg = build_registry({})
    env = _req("mode.set", {"mode": "coding"}, role="pet")
    reply = await reg.dispatch(_ctx(env), env)
    assert reply.payload["code"] == ERR_PERMISSION


async def test_dispatch_invalid_payload_does_not_leak_input() -> None:
    reg = build_registry({})
    env = _req("mode.set", {"mode": {"secret": "hunter2"}})
    reply = await reg.dispatch(_ctx(env), env)
    assert reply.payload["code"] == ERR_VALIDATION
    assert reply.payload["details"]["errors"][0]["loc"] == ["mode"]
    assert "hunter2" not in reply.model_dump_json()


async def test_dispatch_handler_crash_becomes_internal() -> None:
    reg = build_registry({})
    env = _req("mode.set", {"mode": "crash"})
    reply = await reg.dispatch(_ctx(env), env)
    assert reply.payload["code"] == ERR_INTERNAL
    assert "boom" not in reply.payload["message"]


async def test_dispatch_handler_ipc_error_passthrough() -> None:
    reg = build_registry({})
    env = _req("mode.set", {"mode": "forbidden"})
    reply = await reg.dispatch(_ctx(env), env)
    assert reply.payload == {
        "code": ERR_PERMISSION,
        "message": "policy says no",
        "retryable": False,
        "details": {},
    }


async def test_dispatch_response_validated_against_registered_model() -> None:
    """`chat.send` responses are checked against `ChatSendResult`; a handler bug that
    returns the wrong shape becomes `internal`, never a silently malformed response.
    """
    reg = RequestRegistry()

    async def bad_chat_send(ctx: RequestContext, p: BaseModel) -> dict[str, Any]:
        return {"text": "missing request_id/provider/degraded"}

    reg.register("chat.send", EmptyPayload, bad_chat_send)
    env = _req("chat.send", {}, role="dashboard")
    reply = await reg.dispatch(_ctx(env), env)
    assert reply.kind is Kind.ERROR
    assert reply.payload["code"] == ERR_INTERNAL


async def test_dispatch_rejects_non_request_kind() -> None:
    reg = build_registry({})
    env = Envelope(kind=Kind.EVENT, name="state.get", src=Source(role="shell", id="s"))
    reply = await reg.dispatch(_ctx(env), env)
    assert reply.payload["code"] == ERR_VALIDATION


async def test_registration_roles_are_the_authority() -> None:
    """A handler's own `roles` decide, because they sit next to the handler.

    They used to be intersected with the coarse per-role table, which meant a request had to be
    listed in two places - and one that was only listed in one of them was registered, reachable
    in the code, and refused at the door. That is how the stream view became unreachable.
    """
    reg = RequestRegistry()

    async def h(ctx: RequestContext, p: BaseModel) -> dict[str, Any]:
        return {"ok": True}

    reg.register("state.get", ModeSet.__mro__[1], h, roles={"dashboard"})  # BaseModel accepts {}
    assert reg.is_allowed("state.get", "dashboard")
    assert not reg.is_allowed("state.get", "shell")  # registered for dashboard only
    # A name the coarse table never lists is still reachable for the roles it was registered for.
    reg.register("stream.session.status", ModeSet.__mro__[1], h, roles={"dashboard", "shell"})
    assert reg.is_allowed("stream.session.status", "dashboard")
    assert not reg.is_allowed("stream.session.status", "plugin")


async def test_without_explicit_roles_the_per_role_table_decides() -> None:
    reg = RequestRegistry()

    async def h(ctx: RequestContext, p: BaseModel) -> dict[str, Any]:
        return {"ok": True}

    reg.register("mode.set", ModeSet, h)
    assert reg.is_allowed("mode.set", "dashboard")
    assert not reg.is_allowed("mode.set", "pet")


def test_register_twice_fails() -> None:
    reg = build_registry({})
    with pytest.raises(ValueError):
        reg.register("state.get", ModeSet, build_registry({}).get("state.get").handler)  # type: ignore[union-attr]
    reg.unregister("state.get")
    assert "state.get" not in reg.names()


def test_ipc_error_payload_round_trip() -> None:
    err = IpcError("permission.denied", "no", retryable=True, details={"k": 1})
    again = IpcError.from_payload(err.to_payload())
    assert (again.code, again.message, again.retryable, again.details) == (
        "permission.denied",
        "no",
        True,
        {"k": 1},
    )
    assert IpcError.from_payload({"garbage": 1}).code == ERR_INTERNAL
