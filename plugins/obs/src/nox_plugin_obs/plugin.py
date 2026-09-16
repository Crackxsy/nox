"""OBS Studio plugin (ST-11-02/03, Spec v0.2 Stream Bot; ST-15-02, Spec v0.6 Clip Pipeline).

Wraps one `ObsWebSocketClient` (see `nox_plugin_obs.ws_client`) and exposes it as: seven tools
(`obs.status.read`, `obs.scenes.list`, `obs.scene.switch`, `obs.privacy_scene.activate`,
`obs.preflight.check`, `obs.replay_buffer.save`, `obs.replay_buffer.status.read`), the events named
in `manifest.yaml` (`obs.connected/disconnected`, `obs.scene_changed`, `stream.started/ended`
derived from OBS's `StreamStateChanged`, `obs.recording_changed` from `RecordStateChanged`), and a
`security.panic` listener that switches to the configured privacy scene without going through the
normal confirm step (Spec v0.2 §3.6.3 - the trigger is already an explicit, audited user action;
blocking on a confirm dialog nobody may be present to answer would defeat the point).

Health is reported honestly by the caller via `ObsPlugin.health()`: AVAILABLE only once actually
connected and identified with OBS; LIMITED when identified but `config.subscribe_events` is off (no
event stream, so scene/stream-state changes are only ever seen on the next tool call); UNAVAILABLE
when there is no secret to authenticate with, or OBS is simply not reachable.

`obs.replay_buffer.save`/`.status.read` are the Clip Pipeline's only path into OBS (Spec v0.6 §6.1):
the `clips` plugin has no cross-plugin tool call available in the Plugin API, so it never talks to
OBS itself - it only emits `clip.requested`, and the core-side `nox.clips` service calls these two
tools through `ToolExecutor`. Both are read-only/replay-buffer-only: neither can stop the stream,
stop the recording, or touch the recording file - those verbs do not exist in this plugin at all.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from nox.core.events import HealthStatus
from nox.plugins.api import PluginApi
from nox.security.model import Risk

from .ws_client import (
    DEFAULT_EVENT_SUBSCRIPTIONS,
    ObsRequestError,
    ObsWebSocketClient,
)

OBS_PASSWORD_SECRET = "nox/obs/websocket_password"  # noqa: S105 - a secret *name*, not a value

# §3.1's pre-flight items the `obs` plugin cannot observe by itself; reported "unknown" rather than
# faked green/amber/red (ENGINEERING.md "no fake implementations").
NON_OBS_PREFLIGHT_ITEMS = (
    "twitch_token",
    "mic_level",
    "camera",
    "nox_health",
    "ai_budget",
    "network",
    "stream_device_routing",
)


class EmptyInput(BaseModel):
    """Tools that take no arguments."""


class SceneSwitchInput(BaseModel):
    """Input of `obs.scene.switch`. `target` must be a scene from `config.scene_set` (Spec v0.2
    §9): the allow-list check runs in the handler because `scene_set` is runtime config, not a
    schema-time constant, but the effect is the same - an out-of-set target is a validation
    failure, not a permission decision."""

    target: str = Field(..., min_length=1, max_length=200)


class ReplayBufferSaveInput(BaseModel):
    """`obs.replay_buffer.save` takes no OBS-side parameters (obs-websocket's `SaveReplayBuffer`
    has none); `reason` is caller-side context for logs/audit only, never sent to OBS."""

    reason: str = ""


class ObsPlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        self._current_scene = ""
        self._streaming = False
        self._session_id = ""
        self._session_started_at = 0.0
        #: Futures waiting on the next `ReplayBufferSaved` event (Spec v0.6 §4.1 step 4-5): OBS's
        #: `SaveReplayBuffer` response carries no path, only the later event does.
        self._replay_waiters: list[asyncio.Future[str]] = []
        self._replay_save_timeout_s = float(api.config.get("replay_save_timeout_s", 10.0))
        subscribe = bool(api.config.get("subscribe_events", True))
        url = self._url()
        self.client = ObsWebSocketClient(
            url,
            password_provider=self._get_password,
            on_event=self._on_obs_event,
            on_connected=self._on_obs_connected,
            on_disconnected=self._on_obs_disconnected,
            event_subscriptions=DEFAULT_EVENT_SUBSCRIPTIONS if subscribe else 0,
            min_backoff_s=float(api.config.get("min_backoff_s", 1.0)),
            max_backoff_s=float(api.config.get("max_backoff_s", 30.0)),
            authorize=lambda: self._authorize_egress(url),
        )

    def _url(self) -> str:
        host = str(self.api.config.get("host", "127.0.0.1"))
        port = int(self.api.config.get("port", 4455))
        return f"ws://{host}:{port}"

    def _authorize_egress(self, url: str) -> None:
        """Raw `websockets` connections bypass `PluginApi.http()`'s guard, so this plugin enforces
        the scoped `EgressGuard` (manifest `network.egress` + privacy mode) itself before every
        connection attempt (ADR-013)."""
        parts = urlsplit(url)
        self.api.egress.authorize(parts.hostname or "", parts.port or 4455, scheme="ws")

    async def _get_password(self) -> str | None:
        return await self.api.secrets.get(OBS_PASSWORD_SECRET)

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        self.api.events.on("security.panic", self._on_panic)
        self.client.start()

    async def stop(self) -> None:
        await self.client.stop()

    # -- health --------------------------------------------------------------------------------

    async def health(self) -> tuple[HealthStatus, str]:
        if not (self.client.connected and self.client.identified):
            if await self._get_password() is None:
                return HealthStatus.UNAVAILABLE, "no secret configured: " + OBS_PASSWORD_SECRET
            return HealthStatus.UNAVAILABLE, self.client.last_error or "not connected to OBS"
        if self.client.event_subscriptions == 0:
            return HealthStatus.LIMITED, "connected but not subscribed to OBS events"
        return HealthStatus.AVAILABLE, "connected"

    # -- OBS event handling ----------------------------------------------------------------------

    async def _on_obs_connected(self) -> None:
        await self.api.events.emit("obs.connected", {"reason": ""})

    async def _on_obs_disconnected(self, reason: str) -> None:
        await self.api.events.emit(
            "obs.disconnected", {"reason": reason, "backoff_s": self.client.backoff_s}
        )

    async def _on_obs_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "CurrentProgramSceneChanged":
            previous, self._current_scene = self._current_scene, str(data.get("sceneName", ""))
            await self.api.events.emit(
                "obs.scene_changed",
                {"previous_scene": previous, "current_scene": self._current_scene, "by": "manual"},
            )
        elif event_type == "StreamStateChanged":
            await self._on_stream_state(bool(data.get("outputActive", False)))
        elif event_type == "RecordStateChanged":
            await self.api.events.emit(
                "obs.recording_changed",
                {"recording": bool(data.get("outputActive", False)), "by": "obs_detected"},
            )
        elif event_type == "ReplayBufferSaved":
            path = str(data.get("savedReplayPath", ""))
            waiters, self._replay_waiters = self._replay_waiters, []
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(path)

    async def _on_stream_state(self, active: bool) -> None:
        if active and not self._streaming:
            self._streaming = True
            self._session_id = uuid.uuid4().hex
            self._session_started_at = time.monotonic()
            await self.api.events.emit(
                "stream.started",
                {
                    "session_id": self._session_id,
                    "mode": "live",
                    "obs_connected": True,
                    "twitch_connected": False,
                },
            )
        elif not active and self._streaming:
            self._streaming = False
            duration = time.monotonic() - self._session_started_at
            await self.api.events.emit(
                "stream.ended",
                {
                    "session_id": self._session_id,
                    "duration_s": duration,
                    "summary": "",
                    "ended_reason": "manual",
                },
            )

    # -- security.panic --------------------------------------------------------------------------

    async def _on_panic(self, _name: str, _payload: dict[str, Any]) -> None:
        """Spec v0.2 §3.6.3: switch to the privacy scene without the normal `confirm` step. Never
        touches the stream output itself - if OBS is unreachable this only logs and reports on the
        private channel via the plugin log; `stream.stop`/`recording.delete` are hard prohibitions
        and are never attempted, here or anywhere else in this plugin."""
        scene = str(self.api.config.get("privacy_scene", ""))
        if not scene:
            self.api.log.warning("obs.panic_no_privacy_scene")
            return
        try:
            await self.client.request("SetCurrentProgramScene", {"sceneName": scene})
        except Exception as exc:  # noqa: BLE001 - panic must never raise into the event bus
            self.api.log.warning("obs.panic_scene_switch_failed", error=str(exc))

    # -- tools -------------------------------------------------------------------------------------

    async def status_read(self, _data: EmptyInput) -> dict[str, Any]:
        """`obs.status.read`: full scene/source/stream-state snapshot (Spec v0.2 §9)."""
        if not (self.client.connected and self.client.identified):
            return {"connected": False, "reason": self.client.last_error or "not connected"}
        scenes = await self.client.request("GetSceneList")
        stream_status = await self.client.request("GetStreamStatus")
        record_status = await self.client.request("GetRecordStatus")
        return {
            "connected": True,
            "current_scene": scenes.get("currentProgramSceneName", ""),
            "scenes": [s.get("sceneName", "") for s in scenes.get("scenes", [])],
            "streaming": bool(stream_status.get("outputActive", False)),
            "recording": bool(record_status.get("outputActive", False)),
        }

    async def scenes_list(self, _data: EmptyInput) -> dict[str, Any]:
        """`obs.scenes.list`: read-only scene inventory."""
        if not (self.client.connected and self.client.identified):
            raise ConnectionError(
                f"not connected to OBS ({self.client.last_error or 'unavailable'})"
            )
        result = await self.client.request("GetSceneList")
        return {
            "current_scene": result.get("currentProgramSceneName", ""),
            "scenes": [s.get("sceneName", "") for s in result.get("scenes", [])],
        }

    async def scene_switch(self, data: SceneSwitchInput) -> dict[str, Any]:
        """`obs.scene.switch`: medium risk, confirmed in the `stream` profile. Only ever within the
        allow-listed Nox scene set - never `obs.scene.delete`/`rename`, which do not exist here."""
        scene_set = [str(s) for s in self.api.config.get("scene_set", [])]
        if scene_set and data.target not in scene_set:
            raise ValueError(f"{data.target!r} is not in the configured Nox scene set")
        await self.client.request("SetCurrentProgramScene", {"sceneName": data.target})
        return {"target": data.target, "ok": True}

    async def privacy_scene_activate(self, _data: EmptyInput) -> dict[str, Any]:
        """`obs.privacy_scene.activate`: the confirmable, manually-triggered twin of the
        `security.panic` path above - same effect, normal permission gate."""
        scene = str(self.api.config.get("privacy_scene", ""))
        if not scene:
            raise ValueError("no privacy_scene configured")
        await self.client.request("SetCurrentProgramScene", {"sceneName": scene})
        return {"target": scene, "ok": True}

    async def preflight_check(self, _data: EmptyInput) -> dict[str, Any]:
        """`obs.preflight.check`: draft of the §3.1 pre-flight items this plugin can actually judge;
        everything else comes back "unknown" rather than a fabricated green."""
        connected = self.client.connected and self.client.identified
        items: list[dict[str, Any]] = [
            {
                "name": "obs_websocket",
                "status": "green" if connected else "red",
                "detail": "connected" if connected else (self.client.last_error or "not connected"),
            }
        ]
        items.append(await self._scene_set_item(connected))
        items.extend(
            {"name": name, "status": "unknown", "detail": "not observable by the obs plugin"}
            for name in NON_OBS_PREFLIGHT_ITEMS
        )
        known = [i for i in items if i["status"] in ("green", "amber", "red")]
        if any(i["status"] == "red" for i in known):
            overall = "red"
        elif any(i["status"] == "amber" for i in known):
            overall = "amber"
        else:
            overall = "green"
        return {"items": items, "overall": overall}

    async def _scene_set_item(self, connected: bool) -> dict[str, Any]:
        scene_set = [str(s) for s in self.api.config.get("scene_set", [])]
        if not connected or not scene_set:
            return {
                "name": "scene_set",
                "status": "unknown",
                "detail": "OBS not connected" if not connected else "no scene_set configured",
            }
        try:
            result = await self.client.request("GetSceneList")
        except (ConnectionError, ObsRequestError) as exc:
            return {"name": "scene_set", "status": "amber", "detail": str(exc)}
        live = {s.get("sceneName", "") for s in result.get("scenes", [])}
        missing = [s for s in scene_set if s not in live]
        return {
            "name": "scene_set",
            "status": "green" if not missing else "amber",
            "detail": "all configured scenes present" if not missing else f"missing: {missing}",
        }

    # -- Clip Pipeline (Spec v0.6 §6.1, ST-15-02) -----------------------------------------------

    async def replay_buffer_status_read(self, _data: EmptyInput) -> dict[str, Any]:
        """`obs.replay_buffer.status.read`: read-only, honest about "not connected"."""
        if not (self.client.connected and self.client.identified):
            return {"active": False, "reason": self.client.last_error or "not connected"}
        try:
            status = await self.client.request("GetReplayBufferStatus")
        except ObsRequestError as exc:
            return {"active": False, "reason": str(exc)}
        return {"active": bool(status.get("outputActive", False)), "reason": ""}

    async def replay_buffer_save(self, data: ReplayBufferSaveInput) -> dict[str, Any]:
        """`obs.replay_buffer.save`: triggers OBS's own replay buffer and resolves the file it
        writes. Every expected failure (not connected, buffer disabled, no event in time) is
        returned as a structured `{"ok": False, "reason": ...}` result rather than raised, so the
        caller (dashboard, `nox.clips` service) gets a clear, non-crashing message (ST-15-02 AC)."""
        if not (self.client.connected and self.client.identified):
            return {
                "ok": False,
                "file_path": None,
                "reason": self.client.last_error or "not connected to OBS",
            }
        try:
            status = await self.client.request("GetReplayBufferStatus")
        except ObsRequestError as exc:
            return {"ok": False, "file_path": None, "reason": str(exc)}
        if not bool(status.get("outputActive", False)):
            return {
                "ok": False,
                "file_path": None,
                "reason": "OBS's replay buffer is not enabled/active",
            }

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[str] = loop.create_future()
        self._replay_waiters.append(waiter)
        try:
            await self.client.request("SaveReplayBuffer")
        except ObsRequestError as exc:
            if waiter in self._replay_waiters:
                self._replay_waiters.remove(waiter)
            return {"ok": False, "file_path": None, "reason": str(exc)}
        try:
            path = await asyncio.wait_for(waiter, timeout=self._replay_save_timeout_s)
        except TimeoutError:
            if waiter in self._replay_waiters:
                self._replay_waiters.remove(waiter)
            return {
                "ok": False,
                "file_path": None,
                "reason": "OBS did not report a saved replay file in time",
            }
        return {"ok": True, "file_path": path, "reason": ""}


def create(api: PluginApi) -> ObsPlugin:
    plugin = ObsPlugin(api)
    api.tools.register(
        "obs.status.read",
        EmptyInput,
        plugin.status_read,
        Risk.READ,
        description="Full OBS scene/source/stream-state snapshot.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "obs.scenes.list",
        EmptyInput,
        plugin.scenes_list,
        Risk.READ,
        description="List OBS scenes and the current program scene.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "obs.scene.switch",
        SceneSwitchInput,
        plugin.scene_switch,
        Risk.MEDIUM,
        description="Switch OBS to a scene from the configured Nox scene set.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "obs.privacy_scene.activate",
        EmptyInput,
        plugin.privacy_scene_activate,
        Risk.MEDIUM,
        description="Switch OBS to the configured privacy scene.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "obs.preflight.check",
        EmptyInput,
        plugin.preflight_check,
        Risk.READ,
        description="Draft pre-flight check for the items OBS can answer (Spec v0.2 §3.1).",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "obs.replay_buffer.save",
        ReplayBufferSaveInput,
        plugin.replay_buffer_save,
        Risk.MEDIUM,
        description="Save OBS's replay buffer to disk and resolve the written file path.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "obs.replay_buffer.status.read",
        EmptyInput,
        plugin.replay_buffer_status_read,
        Risk.READ,
        description="Read whether OBS's replay buffer is currently active.",
        side_effects=False,
        local=True,
    )
    return plugin
