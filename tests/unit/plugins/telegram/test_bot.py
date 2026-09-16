"""`TelegramBotClient` against the fake Bot API: long-poll offset semantics (which is the
transport-level half of replay protection), send, and the failure paths - including that the bot
token never appears in `last_error` or in an exception message (ENGINEERING.md: no secrets in logs).
"""

from __future__ import annotations

import httpx
import pytest
from nox_plugin_telegram.bot import NO_TOKEN, TelegramApiError, TelegramBotClient

from .fake_telegram import BOT_TOKEN, FakeTelegram


async def _async(value):
    return value


def make_client(telegram: FakeTelegram, *, token: str | None = BOT_TOKEN):
    seen: list[tuple[int, str, str, str]] = []

    async def on_message(update_id: int, sender_id: str, chat_id: str, text: str) -> None:
        seen.append((update_id, sender_id, chat_id, text))

    client = TelegramBotClient(
        api_base="https://api.telegram.org",
        token_provider=lambda: _async(token),
        client_factory=lambda: httpx.AsyncClient(transport=telegram.transport()),
        on_message=on_message,
        poll_timeout_s=0,
        request_timeout_s=2.0,
        min_backoff_s=0.01,
        max_backoff_s=0.02,
    )
    return client, seen


async def test_get_updates_dispatches_message_and_advances_offset(telegram: FakeTelegram):
    client, seen = make_client(telegram)
    telegram.queue_message("/status", sender_id="42", chat_id="42")

    for update in await client.get_updates():
        await client._dispatch(update)

    assert seen == [(1001, "42", "42", "/status")]
    assert client.offset == 1001
    # The next poll acknowledges 1001, which is what makes the same update undeliverable again.
    await client.get_updates()
    assert telegram.offsets == [0, 1002]


async def test_replayed_update_is_not_redelivered_by_the_transport(telegram: FakeTelegram):
    client, seen = make_client(telegram)
    telegram.queue_message("/kill")
    for update in await client.get_updates():
        await client._dispatch(update)
    # An attacker re-queues the very same update after it was acknowledged.
    telegram.queue_raw({"update_id": 1001, "message": {"text": "/kill"}})

    delivered = await client.get_updates()

    assert delivered == []
    assert len(seen) == 1


async def test_non_message_updates_still_advance_the_watermark(telegram: FakeTelegram):
    client, seen = make_client(telegram)
    telegram.queue_raw({"update_id": 2000, "edited_message": {"text": "ignored"}})

    for update in await client.get_updates():
        await client._dispatch(update)

    assert seen == []
    assert client.offset == 2000  # otherwise the ignored update would be polled forever


async def test_send_message_reaches_the_api(telegram: FakeTelegram):
    client, _ = make_client(telegram)

    await client.send_message("42", "hallo")

    assert telegram.sent == [("42", "hallo")]


async def test_missing_token_is_reported_by_name_not_faked(telegram: FakeTelegram):
    client, _ = make_client(telegram, token=None)

    with pytest.raises(TelegramApiError) as exc:
        await client.get_updates()

    assert str(exc.value) == NO_TOKEN


async def test_api_error_is_surfaced_without_the_token(telegram: FakeTelegram):
    client, _ = make_client(telegram)
    telegram.fail_with = f"Unauthorized for https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

    with pytest.raises(TelegramApiError) as exc:
        await client.get_updates()

    assert BOT_TOKEN not in str(exc.value)
    assert "<token>" in str(exc.value)


async def test_transport_error_is_surfaced_without_the_token(telegram: FakeTelegram):
    client, _ = make_client(telegram)
    telegram.raise_transport_error = True

    with pytest.raises(TelegramApiError) as exc:
        await client.get_updates()

    assert BOT_TOKEN not in str(exc.value)
