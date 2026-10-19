"""A plugin client follows only the events its manifest declares, whatever it asks the hub for."""

from __future__ import annotations

from nox.ipc.protocol import NAME_SUBSCRIBE, Kind
from nox.ipc.server import IpcHub
from nox.ipc.tokens import TokenStore

from .conftest import RawClient


async def plugin_client(raw: object, tokens: TokenStore, plugin_id: str) -> RawClient:
    client: RawClient = await raw("plugin", f"plugin:{plugin_id}")  # type: ignore[operator]
    reply = await client.auth(tokens.issue_worker_token(f"plugin:{plugin_id}"))
    assert reply.kind is Kind.RESPONSE, reply.payload
    return client


async def test_a_plugin_gets_only_what_it_declared(
    hub: IpcHub, raw: object, tokens: TokenStore
) -> None:
    hub.plugin_subscriptions = lambda client_id: (
        ("security.*", "x.y") if client_id == "plugin:dice" else ()
    )
    client = await plugin_client(raw, tokens, "dice")
    asked = ["*", "**", "voice.*", "voice.transcript_ready", "twitch.chat_message", "x.y",
             "security.*", "security.kill_switch"]  # fmt: skip
    reply = await client.request(NAME_SUBSCRIBE, {"patterns": asked})
    assert reply.kind is Kind.RESPONSE
    assert reply.payload["patterns"] == ["system.*", "x.y", "security.*", "security.kill_switch"]


async def test_without_a_policy_a_plugin_gets_the_system_defaults_only(
    hub: IpcHub, raw: object, tokens: TokenStore
) -> None:
    client = await plugin_client(raw, tokens, "dice")
    reply = await client.request(NAME_SUBSCRIBE, {"patterns": ["*", "voice.*"]})
    assert reply.payload["patterns"] == ["system.*"]
