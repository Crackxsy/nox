"""The stream overlay's persistent, read-only credential (IPC role `overlay`): stored as a hash,
survives a restart, revocable, and good for nothing but watching."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from nox.core.events import Event
from nox.ipc.dispatch import ROLE_ALLOWLIST
from nox.ipc.overlay_tokens import OBS_LABEL, OverlayTokenStore
from nox.ipc.protocol import Kind
from nox.ipc.server import IpcHub, role_may_see
from nox.ipc.tokens import TokenStore
from tests.unit.ipc.conftest import RawClient, SimpleBus

Raw = Callable[..., Awaitable[RawClient]]


@pytest.fixture
def store(tmp_path: Path) -> OverlayTokenStore:
    return OverlayTokenStore(tmp_path / "overlay_tokens.json")


@pytest.fixture
def tokens(store: OverlayTokenStore) -> TokenStore:
    # Overrides the conftest fixture: the hub under test knows overlay credentials.
    return TokenStore(overlay=store)


# ---- the store -------------------------------------------------------------------------------


def test_only_a_hash_is_written_and_it_survives_a_restart(
    store: OverlayTokenStore, tmp_path: Path
) -> None:
    credential, token = store.issue()
    text = (tmp_path / "overlay_tokens.json").read_text(encoding="utf-8")
    assert token not in text
    assert json.loads(text)["credentials"][0]["sha256"]
    reloaded = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    assert reloaded.verify(token) == credential


def test_issuing_again_replaces_the_old_credential(store: OverlayTokenStore) -> None:
    _, old = store.issue()
    _, new = store.issue()
    assert store.verify(old) is None and store.verify(new) is not None


def test_adopt_makes_a_pending_credential_the_only_one(store: OverlayTokenStore) -> None:
    first, old = store.issue(OBS_LABEL)
    pending, new = store.issue("obs-pending")
    assert store.verify(old) is not None  # the old overlay keeps working until adoption
    assert store.adopt(pending.credential_id, OBS_LABEL) == [first.credential_id]
    assert store.verify(old) is None
    assert store.verify(new) is not None and store.verify(new).label == OBS_LABEL  # type: ignore[union-attr]


def test_revoked_credential_is_rejected(store: OverlayTokenStore, tmp_path: Path) -> None:
    credential, token = store.issue()
    assert store.revoke(credential.credential_id)
    assert store.verify(token) is None
    assert OverlayTokenStore(tmp_path / "overlay_tokens.json").verify(token) is None


def test_a_damaged_file_accepts_nothing(tmp_path: Path) -> None:
    path = tmp_path / "overlay_tokens.json"
    path.write_text("{not json", encoding="utf-8")
    assert OverlayTokenStore(path).verify("anything") is None


# ---- authentication ----------------------------------------------------------------------------


def test_overlay_role_takes_only_an_overlay_credential(
    tokens: TokenStore, store: OverlayTokenStore
) -> None:
    credential, token = store.issue()
    decision = tokens.authenticate(token, "overlay", "overlay:1")
    assert decision.ok and decision.kind == "overlay"
    assert decision.credential_id == credential.credential_id
    # The session token is no overlay credential...
    assert not tokens.authenticate(tokens.session_token, "overlay", "overlay:1").ok
    # ...and an overlay credential opens no other role.
    for role in ("pet", "dashboard", "shell", "worker", "plugin"):
        assert not tokens.authenticate(token, role, f"{role}:1").ok


def test_overlay_role_may_only_watch() -> None:
    assert ROLE_ALLOWLIST["overlay"] == ("ipc.*", "state.get")
    assert role_may_see("overlay", "pet.state")
    assert role_may_see("overlay", "tts.started")
    for name in (
        "voice.transcript_final",
        "chat.message",
        "twitch.chat_message",
        "memory.saved",
        "security.audit",
        "obs.scene_changed",
        "ai.response_delta",
    ):
        assert not role_may_see("overlay", name), name


async def test_overlay_connection_cannot_call_anything_that_changes(
    hub: IpcHub, raw: Raw, store: OverlayTokenStore
) -> None:
    _, token = store.issue()
    client = await raw("overlay", "overlay:1")
    assert (await client.auth(token)).kind is Kind.RESPONSE
    for name in (
        "mode.set",
        "security.kill",
        "security.panic",
        "privacy.set",
        "chat.send",
        "voice.mute",
        "pet.interact",
        "security.permission.reply",
        "worker.register",
    ):
        reply = await client.request(name, {})
        assert reply.kind is Kind.ERROR and reply.payload["code"] == "permission.denied", name
    reply = await client.request("state.get", {})
    assert reply.kind is Kind.RESPONSE


async def test_revoking_disconnects_and_locks_out(
    hub: IpcHub, raw: Raw, store: OverlayTokenStore
) -> None:
    credential, token = store.issue()
    client = await raw("overlay", "overlay:1")
    assert (await client.auth(token)).kind is Kind.RESPONSE
    store.revoke(credential.credential_id)
    assert await hub.disconnect_credential([credential.credential_id]) == 1
    await client.wait_closed()
    again = await raw("overlay", "overlay:2")
    reply = await again.auth(token)
    assert reply.kind is Kind.ERROR


async def test_overlay_receives_pet_events_but_not_chat(
    hub: IpcHub, raw: Raw, store: OverlayTokenStore, bus: SimpleBus
) -> None:
    _, token = store.issue()
    client = await raw("overlay", "overlay:1")
    assert (await client.auth(token)).kind is Kind.RESPONSE
    reply = await client.request("ipc.subscribe", {"patterns": ["*", "**"]})
    assert reply.kind is Kind.RESPONSE
    await bus.publish(Event(name="twitch.chat_message", payload={"text": "secret"}))
    await bus.publish(Event(name="pet.state", payload={"mood": "happy"}))
    received = await client.recv_named("pet.state")
    assert received.payload == {"mood": "happy"}
    with pytest.raises(asyncio.TimeoutError):
        await client.recv_named("twitch.chat_message", timeout=0.2)
