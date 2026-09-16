"""Twitch chat bot plugin (ST-11-04, Spec v0.2 Stream Bot).

Wraps one `TwitchIrcClient` (see `nox_plugin_twitch.irc_client`) and exposes it as: two tools
(`twitch.chat.send`, `twitch.chat.status.read`), the events named in `manifest.yaml`
(`twitch.connected/disconnected`, `twitch.chat_message` scored by a `RelevanceClassifier`,
`twitch.command_invoked`, `stream.funken_awarded`, `stream.minigame_started/ended`), and the
built-in `!rps`/`!funken`/`!hilfe`/`!help` chat commands, all answered through the same
`_send` path as the `twitch.chat.send` tool (rate limiter + `ModerationGate`, never bypassed).

Health is reported honestly by `TwitchPlugin.health()`: AVAILABLE only once actually connected and
joined; UNAVAILABLE when the plugin has no `nox/twitch/oauth_token`/`nox/twitch/bot_username`
secret yet (ES-01: no bot account exists until the operator creates one) or Twitch is unreachable.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from nox.core.events import HealthStatus
from nox.ipc.errors import ERR_PERMISSION, ERR_RATE_LIMITED, IpcError
from nox.plugins.api import PluginApi
from nox.security.model import Risk

from .irc_client import TwitchIrcClient
from .moderation import ModerationGate
from .protocol import ChatTags
from .ratelimit import RateLimiter
from .relevance import RelevanceClassifier
from .rps import ALIASES as RPS_ALIASES
from .rps import GERMAN_ALIASES as RPS_GERMAN_ALIASES
from .rps import RockPaperScissors, RpsResult
from .settings import resolve_settings

TWITCH_OAUTH_SECRET = "nox/twitch/oauth_token"  # noqa: S105 - a secret *name*, not a value
TWITCH_USERNAME_SECRET = "nox/twitch/bot_username"  # noqa: S105 - a secret *name*, not a value


class EmptyInput(BaseModel):
    """Tools that take no arguments."""


class ChatSendInput(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


def _help_text(lang: str) -> str:
    if lang == "de":
        return "Befehle: !rps <stein|schere|papier>, !funken, !hilfe"
    return "Commands: !rps <rock|paper|scissors>, !funken, !help"


class TwitchPlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        self._chat_event_id = 0
        #: `stream.twitch.*` from the configuration, with the deprecated manifest keys still
        #: honoured for one release (#26, see `nox_plugin_twitch.settings`).
        self.settings = resolve_settings(api)
        self._channel = self.settings.channel

        self.relevance = RelevanceClassifier(
            list(self.settings.bot_names), cooldown_s=self.settings.relevance_cooldown_s
        )
        self.moderation = ModerationGate(
            blocklist=[str(b) for b in api.config.get("moderation_blocklist", [])]
        )
        self.rate_limiter = RateLimiter(
            max_messages=self.settings.rate_limit_max_messages,
            window_s=self.settings.rate_limit_window_s,
            min_gap_s=self.settings.rate_limit_min_gap_s,
        )
        self.rps = RockPaperScissors(
            cooldown_s=float(api.config.get("rps_cooldown_s", 30.0)),
            win_funken=float(api.config.get("rps_win_funken", 5.0)),
        )

        host = str(api.config.get("host", "irc.chat.twitch.tv"))
        port = int(api.config.get("port", 6697))
        self.client = TwitchIrcClient(
            host,
            port,
            channel=self._channel,
            token_provider=self._get_token,
            nick_provider=self._get_nick,
            on_privmsg=self._on_privmsg,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            tls=bool(api.config.get("tls", True)),
            min_backoff_s=self.settings.min_backoff_s,
            max_backoff_s=self.settings.max_backoff_s,
            authorize=lambda: self.api.egress.authorize(host, port, scheme="irc"),
        )
        self._commands: dict[str, Any] = {
            "rps": self._cmd_rps,
            "funken": self._cmd_funken,
            "hilfe": self._cmd_help_de,
            "help": self._cmd_help,
        }

    async def _get_token(self) -> str | None:
        return await self.api.secrets.get(TWITCH_OAUTH_SECRET)

    async def _get_nick(self) -> str | None:
        return await self.api.secrets.get(TWITCH_USERNAME_SECRET)

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        self.client.start()

    async def stop(self) -> None:
        await self.client.stop()

    # -- health --------------------------------------------------------------------------------

    async def health(self) -> tuple[HealthStatus, str]:
        if not (self.client.connected and self.client.joined):
            if await self._get_token() is None or await self._get_nick() is None:
                return (
                    HealthStatus.UNAVAILABLE,
                    f"no credentials configured: {TWITCH_OAUTH_SECRET}, {TWITCH_USERNAME_SECRET}",
                )
            return HealthStatus.UNAVAILABLE, self.client.last_error or "not connected to Twitch"
        return HealthStatus.AVAILABLE, "connected"

    # -- IRC event handling ----------------------------------------------------------------------

    async def _on_connected(self) -> None:
        await self.api.events.emit("twitch.connected", {"reason": ""})

    async def _on_disconnected(self, reason: str) -> None:
        await self.api.events.emit(
            "twitch.disconnected", {"reason": reason, "backoff_s": self.client.backoff_s}
        )

    async def _on_privmsg(self, tags: ChatTags, login: str, _channel: str, text: str) -> None:
        self._chat_event_id += 1
        chat_event_id = self._chat_event_id
        viewer_id = tags.user_id or login

        relevance, addressed = self.relevance.classify(viewer_id, text)
        await self.api.events.emit(
            "twitch.chat_message",
            {
                "chat_event_id": chat_event_id,
                "viewer_id": viewer_id,
                "text": text,
                "channel": "public",
                "relevance": relevance,
                "addressed_to_nox": addressed,
            },
        )
        if text.startswith("!"):
            await self._handle_command(chat_event_id, viewer_id, text)

    async def _handle_command(self, chat_event_id: int, viewer_id: str, text: str) -> None:
        parts = text[1:].split()
        if not parts:
            return
        command, args = parts[0].lower(), parts[1:]
        await self.api.events.emit(
            "twitch.command_invoked",
            {
                "chat_event_id": chat_event_id,
                "viewer_id": viewer_id,
                "command": command,
                "args": args,
            },
        )
        handler = self._commands.get(command)
        if handler is not None:
            try:
                await handler(viewer_id, args)
            except Exception as exc:  # noqa: BLE001 - a blocked/rate-limited reply must never tear
                # down the IRC read loop (it would look like a disconnect and force a reconnect).
                self.api.log.warning("twitch.command_reply_failed", command=command, error=str(exc))

    # -- built-in commands -------------------------------------------------------------------------

    async def _cmd_help(self, _viewer_id: str, _args: list[str]) -> None:
        """`!help` - English reply. `!hilfe` is wired to `_cmd_help_de` instead (`_commands` in
        `__init__`): the dispatcher only ever hands a handler `(viewer_id, args)`, not the alias."""
        await self._send(_help_text("en"))

    async def _cmd_help_de(self, _viewer_id: str, _args: list[str]) -> None:
        """`!hilfe` - German reply, see `_cmd_help`."""
        await self._send(_help_text("de"))

    async def _cmd_funken(self, _viewer_id: str, _args: list[str]) -> None:
        """Balances live in the core's Funken ledger, not here (Spec v0.2 §3.5: plugins never book
        Funken). `twitch.command_invoked` was already emitted by `_handle_command`; the core answers
        `!funken` later through its own `twitch.chat.send` call. Faking a balance here would violate
        ENGINEERING.md's "no fake implementations", so this is intentionally a no-op."""
        return

    async def _cmd_rps(self, viewer_id: str, args: list[str]) -> None:
        if not args:
            await self._send("Usage: !rps rock|paper|scissors (or: stein|schere|papier)")
            return
        raw_choice = args[0].lower()
        lang = "de" if raw_choice in RPS_GERMAN_ALIASES else "en"
        choice = RPS_ALIASES.get(raw_choice)
        if choice is None:
            msg = (
                "Ungültige Wahl. Nutzung: !rps stein|schere|papier"
                if lang == "de"
                else "Invalid choice. Usage: !rps rock|paper|scissors"
            )
            await self._send(msg)
            return

        remaining = self.rps.cooldown_remaining(viewer_id)
        if remaining > 0:
            msg = (
                f"Warte noch {remaining:.0f}s bis zum nächsten !rps."
                if lang == "de"
                else f"Wait {remaining:.0f}s before your next !rps."
            )
            await self._send(msg)
            return

        result = self.rps.play(viewer_id, choice)
        session_id = uuid.uuid4().hex
        await self.api.events.emit(
            "stream.minigame_started",
            {"session_id": session_id, "game_id": "rps", "participants": [viewer_id]},
        )
        await self.api.events.emit(
            "stream.minigame_ended",
            {
                "session_id": session_id,
                "game_id": "rps",
                "participants": [viewer_id],
                "result": {
                    "viewer_choice": choice,
                    "bot_choice": result.bot_choice,
                    "outcome": result.outcome,
                },
            },
        )
        await self._send(self._rps_reply(lang, result))
        if result.outcome == "win":
            await self.api.events.emit(
                "stream.funken_awarded",
                {"viewer_id": viewer_id, "delta": self.rps.win_funken, "reason": "rps_win"},
            )

    def _rps_reply(self, lang: str, result: RpsResult) -> str:
        if lang == "de":
            texts = {
                "win": f"Ich habe {result.bot_choice} gewählt - du gewinnst!",
                "lose": f"Ich habe {result.bot_choice} gewählt - ich gewinne!",
                "tie": f"Beide {result.bot_choice} - unentschieden!",
            }
        else:
            texts = {
                "win": f"I chose {result.bot_choice} - you win!",
                "lose": f"I chose {result.bot_choice} - I win!",
                "tie": f"Both {result.bot_choice} - it's a tie!",
            }
        return texts[result.outcome]

    # -- send path (shared by the tool and every built-in command reply) ------------------------

    async def _send(self, text: str) -> dict[str, Any]:
        # `IpcError`, not a plain exception: a plain exception raised inside a tool handler is
        # re-wrapped into a generic "handler failed" message on its way back across the worker's
        # IPC boundary (`nox.ipc.client.IpcClient._answer`'s `except Exception` branch) - only an
        # `IpcError` survives that hop with its real message and a typed code intact.
        ok, reason = self.moderation.check(text)
        if not ok:
            raise IpcError(ERR_PERMISSION, f"message blocked by moderation gate: {reason}")
        allowed, rate_reason = self.rate_limiter.try_acquire()
        if not allowed:
            raise IpcError(ERR_RATE_LIMITED, f"rate limited: {rate_reason}")
        await self.client.send_privmsg(text)
        return {"sent": True, "text": text}

    # -- tools -------------------------------------------------------------------------------------

    async def chat_send(self, data: ChatSendInput) -> dict[str, Any]:
        """`twitch.chat.send`: low risk, rate-limited, moderation-gated."""
        return await self._send(data.text)

    async def chat_status_read(self, _data: EmptyInput) -> dict[str, Any]:
        """`twitch.chat.status.read`: read-only connection status, never fakes "connected"."""
        return {
            "connected": self.client.connected,
            "joined": self.client.joined,
            "channel": self._channel,
            "reason": self.client.last_error,
        }


def create(api: PluginApi) -> TwitchPlugin:
    plugin = TwitchPlugin(api)
    api.tools.register(
        "twitch.chat.send",
        ChatSendInput,
        plugin.chat_send,
        Risk.LOW,
        description="Send a chat message to the configured Twitch channel.",
        side_effects=True,
        local=False,
    )
    api.tools.register(
        "twitch.chat.status.read",
        EmptyInput,
        plugin.chat_status_read,
        Risk.READ,
        description="Read the Twitch IRC connection/join status.",
        side_effects=False,
        local=False,
    )
    return plugin
