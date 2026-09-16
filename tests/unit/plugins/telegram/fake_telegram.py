"""A fake Telegram Bot API, good enough for everything the plugin does (Spec v0.8 §10: unit tests
never touch `api.telegram.org`).

It is an `httpx.MockTransport` handler rather than a socket server: the plugin's only way out is
`PluginApi.http()`, whose `GuardedTransport` wraps exactly the transport the plugin worker injects,
so a mock transport exercises the real client *and* the real egress guard (a request to any host
other than `api.telegram.org:443` is refused by the guard before this fake ever sees it).

It implements `getUpdates` (with the Bot API's real `offset` acknowledgement semantics: an offset
deletes every update below it, which is what makes a replay impossible at the transport level) and
`sendMessage`, plus the two failure shapes the plugin must survive: `ok: false` and a transport
error.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

BOT_TOKEN = "123456:FAKE-TOKEN"  # noqa: S105 - a test fixture value, never a real credential


class FakeTelegram:
    def __init__(self, *, token: str = BOT_TOKEN) -> None:
        self.token = token
        #: Queued inbound updates, oldest first.
        self.updates: list[dict[str, Any]] = []
        #: Everything the plugin sent, as `(chat_id, text)`.
        self.sent: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.offsets: list[int] = []
        #: Set to a description to make the next call fail with `ok: false`.
        self.fail_with: str | None = None
        #: Set to raise a transport error instead of answering.
        self.raise_transport_error = False
        self._next_update_id = 1000

    # -- test helpers ----------------------------------------------------------------------------

    def queue_message(self, text: str, *, sender_id: str = "42", chat_id: str = "42") -> int:
        self._next_update_id += 1
        self.updates.append(
            {
                "update_id": self._next_update_id,
                "message": {
                    "message_id": self._next_update_id,
                    "from": {"id": int(sender_id), "is_bot": False, "first_name": "Alex"},
                    "chat": {"id": int(chat_id), "type": "private"},
                    "date": 1757800000,
                    "text": text,
                },
            }
        )
        return self._next_update_id

    def queue_raw(self, update: dict[str, Any]) -> None:
        self.updates.append(update)

    # -- transport -------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.raise_transport_error:
            raise httpx.ConnectError("fake telegram is down", request=request)
        path = request.url.path
        expected_prefix = f"/bot{self.token}/"
        if not path.startswith(expected_prefix):
            # Wrong or missing token: exactly what the Bot API answers.
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        method = path[len(expected_prefix) :]
        self.calls.append(method)
        payload = json.loads(request.content or b"{}")
        if self.fail_with is not None:
            description, self.fail_with = self.fail_with, None
            return httpx.Response(200, json={"ok": False, "description": description})
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            return httpx.Response(404, json={"ok": False, "description": "method not found"})
        return httpx.Response(200, json={"ok": True, "result": handler(payload)})

    def _m_getUpdates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: N802
        offset = int(payload.get("offset", 0))
        self.offsets.append(offset)
        if offset:
            # Real Bot API semantics: confirming an offset drops every update below it for good.
            self.updates = [u for u in self.updates if int(u["update_id"]) >= offset]
        pending, self.updates = self.updates, []
        return pending

    def _m_sendMessage(self, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        chat_id, text = str(payload.get("chat_id", "")), str(payload.get("text", ""))
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent), "chat": {"id": chat_id}, "text": text}
