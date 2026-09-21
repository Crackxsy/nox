"""Highlight candidate detector (Clip Pipeline).

Scores incoming `rl.event`, `twitch.chat_mood_changed` and `twitch.command_invoked` (`!clip`)
against configurable rules and a per-`trigger_kind` cooldown, and turns a match into a
`clip.requested` event. This plugin never calls OBS itself: the Plugin API has no cross-plugin tool
call (a plugin only ever sees its own `api.tools`, and this manifest declares none), so the core-
side `nox.clips` service is the one that calls `obs.replay_buffer.save` through `ToolExecutor` in
response to `clip.requested` (see `src/nox/clips/service.py`).

Thresholds and cooldowns are configuration (`config.highlight_kinds`, `.chat_hype_threshold`,
`.cooldown_s`, `.manual_lookback_s`), never a code constant, so recalibrating needs no redeploy.
"""

from __future__ import annotations

import time
from typing import Any

from nox.plugins.api import PluginApi

#: `twitch.chat_mood_changed.category` values treated as a hype spike. The
#: event model keeps the category set itself pending approval, so this is a small,
#: named allow-list rather than guessing at every possible category string.
HYPE_CATEGORIES = frozenset({"hype", "hype_spike", "excited", "chaos"})


class ClipsPlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        self._highlight_kinds = {
            str(k).lower() for k in api.config.get("highlight_kinds", []) or []
        }
        self._hype_threshold = float(api.config.get("chat_hype_threshold", 0.75))
        self._cooldown_s = float(api.config.get("cooldown_s", 15.0))
        self._manual_lookback_s = float(api.config.get("manual_lookback_s", 20.0))
        self._session_id = ""
        self._suppressed = False
        # trigger_kind -> monotonic ts of the last accepted request (anti-double-clip).
        self._last_request: dict[str, float] = {}
        # last rl.event seen, for the manual-trigger secondary-tag lookup.
        self._last_rl_event: tuple[float, str] | None = None
        self._clock = time.monotonic

    async def start(self) -> None:
        self.api.events.on("rl.event", self._on_rl_event)
        self.api.events.on("twitch.chat_mood_changed", self._on_chat_mood)
        self.api.events.on("twitch.command_invoked", self._on_command)
        self.api.events.on("stream.started", self._on_stream_started)
        self.api.events.on("stream.ended", self._on_stream_ended)
        self.api.events.on("security.kill_switch", self._on_suppress)
        self.api.events.on("security.panic", self._on_suppress)

    async def stop(self) -> None:
        return None

    # -- cooldown / gating ------------------------------------------------------------------------

    def _on_cooldown(self, trigger_kind: str) -> bool:
        last = self._last_request.get(trigger_kind)
        return last is not None and (self._clock() - last) < self._cooldown_s

    async def _request(
        self,
        *,
        trigger_kind: str,
        source: str,
        origin_event_id: str = "",
        tags: list[str] | None = None,
    ) -> None:
        if self._suppressed or self._on_cooldown(trigger_kind):
            return
        self._last_request[trigger_kind] = self._clock()
        await self.api.events.emit(
            "clip.requested",
            {
                "trigger_kind": trigger_kind,
                "source": source,
                "origin_event_id": origin_event_id,
                "session_id": self._session_id,
                "tags": tags or [],
            },
        )

    # -- event handlers ---------------------------------------------------------------------------

    async def _on_rl_event(self, _name: str, payload: dict[str, Any]) -> None:
        # Defensive: read `kind` as a plain dict field rather than importing `RlEvent` - the rl
        # plugin (built concurrently) is the payload's owner.
        kind = str(payload.get("kind", "")).lower()
        if not kind or kind not in self._highlight_kinds:
            return
        self._last_rl_event = (self._clock(), kind)
        await self._request(trigger_kind=f"rl.{kind}", source="event")

    async def _on_chat_mood(self, _name: str, payload: dict[str, Any]) -> None:
        category = str(payload.get("category", "")).lower()
        # Defensive extra field: a future numeric hype score, if the twitch plugin ever adds one;
        # the categorical allow-list above is the primary signal today.
        score = payload.get("score")
        is_hype = category in HYPE_CATEGORIES or (
            isinstance(score, int | float) and float(score) >= self._hype_threshold
        )
        if not is_hype:
            return
        await self._request(trigger_kind="chat_hype", source="event")

    async def _on_command(self, _name: str, payload: dict[str, Any]) -> None:
        command = str(payload.get("command", "")).lower()
        if command != "clip":
            return
        tags = ["user_marker"]
        if self._last_rl_event is not None:
            ts, kind = self._last_rl_event
            if (self._clock() - ts) <= self._manual_lookback_s:
                tags.append(f"rl.{kind}")
        chat_event_id = payload.get("chat_event_id")
        origin = str(chat_event_id) if chat_event_id is not None else ""
        await self._request(
            trigger_kind="user_marker", source="manual", origin_event_id=origin, tags=tags
        )

    async def _on_stream_started(self, _name: str, payload: dict[str, Any]) -> None:
        self._session_id = str(payload.get("session_id", ""))

    async def _on_stream_ended(self, _name: str, _payload: dict[str, Any]) -> None:
        self._session_id = ""

    async def _on_suppress(self, _name: str, _payload: dict[str, Any]) -> None:
        # -equivalent for this plugin: once the kill switch or panic fires, stop
        # requesting new clips until the plugin is restarted (a fresh, explicit action) rather than
        # silently keep capturing during an emergency stop.
        self._suppressed = True


def create(api: PluginApi) -> ClipsPlugin:
    return ClipsPlugin(api)
