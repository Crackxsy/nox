"""The `telegram` plugin as the core sees it: inbound messages become `remote.message` events with
the sender id and nothing else decided, `telegram.send` is rate-limited and honest about failures,
and the manifest keeps the plugin structurally unable to see transcripts, memory or AI output.
"""

from __future__ import annotations

import pytest
from nox_plugin_telegram.plugin import TelegramSendInput

from nox.ipc.errors import ERR_RATE_LIMITED, ERR_UNAVAILABLE, IpcError
from nox.plugins.manifest import load_manifest
from nox.security.egress import EgressDenied
from nox.security.model import Risk

from .conftest import MANIFEST_PATH, FakeClient, make_api
from .fake_telegram import FakeTelegram

# -- manifest / trust boundary -----------------------------------------------------------------


def test_manifest_declares_only_telegram_egress_and_the_token_name():
    manifest = load_manifest(MANIFEST_PATH, expected_id="telegram")

    assert manifest.network.egress == ["api.telegram.org:443"]
    assert manifest.secrets == ["nox/telegram/bot_token"]
    assert {p.tool: p.risk for p in manifest.permissions} == {
        "telegram.send": Risk.LOW,
        "telegram.status.read": Risk.READ,
    }


def test_manifest_listens_to_nothing_so_transcripts_cannot_be_forwarded():
    """The structural half of "never sends transcripts or secrets": a plugin only receives the
    events it declares, and this one declares none (IPC Model "Outbound filtering")."""
    manifest = load_manifest(MANIFEST_PATH, expected_id="telegram")

    assert manifest.events.listens == []
    assert "remote.message" in manifest.events.emits


async def test_plugin_registers_no_event_handlers(fake_client: FakeClient, plugin):
    assert fake_client.handlers == {}


async def test_egress_guard_refuses_any_other_host(fake_client: FakeClient, telegram: FakeTelegram):
    api = make_api(fake_client, telegram)
    client = api.http()

    with pytest.raises(EgressDenied):
        async with client:
            await client.post("https://example.com/steal", json={})


# -- inbound ------------------------------------------------------------------------------------


async def test_inbound_message_becomes_a_remote_message_event(
    fake_client: FakeClient, telegram: FakeTelegram, plugin
):
    telegram.queue_message("hallo Nox", sender_id="42", chat_id="77")

    for update in await plugin.client.get_updates():
        await plugin.client._dispatch(update)

    assert fake_client.emitted("remote.message") == [
        {
            "channel": "telegram",
            "sender_id": "42",
            "chat_id": "77",
            "update_id": 1001,
            "text": "hallo Nox",
        }
    ]


async def test_plugin_never_answers_a_command_itself(
    fake_client: FakeClient, telegram: FakeTelegram, plugin
):
    """Commands are the core's business (Security Model §2): the plugin forwards `/kill` like any
    other text and sends nothing back on its own."""
    telegram.queue_message("/kill", sender_id="999")

    for update in await plugin.client.get_updates():
        await plugin.client._dispatch(update)

    assert telegram.sent == []
    assert fake_client.emitted("remote.message")[0]["text"] == "/kill"


# -- outbound -----------------------------------------------------------------------------------


async def test_send_uses_the_last_inbound_chat_when_none_is_given(telegram: FakeTelegram, plugin):
    telegram.queue_message("hallo", chat_id="77")
    for update in await plugin.client.get_updates():
        await plugin.client._dispatch(update)

    await plugin.send(TelegramSendInput(text="Not-Aus aktiv."))

    assert telegram.sent == [("77", "Not-Aus aktiv.")]


async def test_send_without_any_chat_is_refused_not_faked(plugin):
    with pytest.raises(IpcError) as exc:
        await plugin.send(TelegramSendInput(text="hallo"))

    assert exc.value.code == ERR_UNAVAILABLE


async def test_send_is_rate_limited(telegram: FakeTelegram, plugin):
    await plugin.send(TelegramSendInput(text="eins", chat_id="42"))

    with pytest.raises(IpcError) as exc:
        await plugin.send(TelegramSendInput(text="zwei", chat_id="42"))

    assert exc.value.code == ERR_RATE_LIMITED
    assert telegram.sent == [("42", "eins")]  # the refused send never reached the API


async def test_long_text_is_truncated_not_dropped(telegram: FakeTelegram, plugin):
    await plugin.send(TelegramSendInput(text="x" * 4000, chat_id="42"))

    _, text = telegram.sent[0]
    assert len(text) <= 3502
    assert text.endswith(" …")


async def test_api_failure_is_reported_as_unavailable(telegram: FakeTelegram, plugin):
    telegram.fail_with = "chat not found"

    with pytest.raises(IpcError) as exc:
        await plugin.send(TelegramSendInput(text="hallo", chat_id="42"))

    assert exc.value.code == ERR_UNAVAILABLE


# -- health -------------------------------------------------------------------------------------


async def test_health_is_unavailable_without_a_token(telegram: FakeTelegram):
    from nox_plugin_telegram import create

    plugin = create(make_api(FakeClient(bot_token=None), telegram))

    status, reason = await plugin.health()

    assert status.value == "unavailable"
    assert "nox/telegram/bot_token" in reason


async def test_health_never_claims_connected_before_polling(plugin):
    status, _ = await plugin.health()

    assert status.value == "unavailable"
