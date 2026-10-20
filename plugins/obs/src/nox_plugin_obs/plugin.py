"""OBS Studio plugin.

Wraps one `ObsWebSocketClient` (see `nox_plugin_obs.ws_client`) and exposes it as: seven tools
(`obs.status.read`, `obs.scenes.list`, `obs.scene.switch`, `obs.privacy_scene.activate`,
`obs.preflight.check`, `obs.replay_buffer.save`, `obs.replay_buffer.status.read`), the events named
in `manifest.yaml` (`obs.connected/disconnected`, `obs.scene_changed`, `stream.started/ended`
derived from OBS's `StreamStateChanged`, `obs.recording_changed` from `RecordStateChanged`), and a
`security.panic` listener that switches to the configured privacy scene without going through the
normal confirm step (the trigger is already an explicit, audited user action; blocking on a confirm
dialog nobody may be present to answer would defeat the point).

Health is reported honestly by the caller via `ObsPlugin.health`: AVAILABLE only once actually
connected and identified with OBS; LIMITED when identified but `config.subscribe_events` is off (no
event stream, so scene/stream-state changes are only ever seen on the next tool call); UNAVAILABLE
when there is no secret to authenticate with, or OBS is simply not reachable.

`obs.replay_buffer.save`/`.status.read` are the Clip Pipeline's only path into OBS:
the `clips` plugin has no cross-plugin tool call available in the Plugin API, so it never talks to
OBS itself - it only emits `clip.requested`, and the core-side `nox.clips` service calls these two
tools through `ToolExecutor`. Both are read-only/replay-buffer-only: neither can stop the stream,
stop the recording, or touch the recording file - those verbs do not exist in this plugin at all.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from nox.core.events import HealthStatus
from nox.plugins.api import PluginApi
from nox.security.model import Risk
from nox.stream.obs_requests import (
    TOOL_GUIDE,
    classify,
    privacy_refusal,
    redact,
    redact_text,
    refusal,
)

from .ws_client import (
    DEFAULT_EVENT_SUBSCRIPTIONS,
    ObsRequestError,
    ObsWebSocketClient,
)

OBS_PASSWORD_SECRET = "nox/obs/websocket_password"  # noqa: S105 - a secret *name*, not a value

# pre-flight items the `obs` plugin cannot observe by itself; reported "unknown" rather than
# faked green/amber/red (the project standards "no fake implementations").
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
    """Input of `obs.scene.switch`. `target` must be a scene from `config.scene_set`: the allow-
    list check runs in the handler because `scene_set` is runtime config, not a
    schema-time constant, but the effect is the same - an out-of-set target is a validation
    failure, not a permission decision."""

    target: str = Field(..., min_length=1, max_length=200)


class ReplayBufferSaveInput(BaseModel):
    """`obs.replay_buffer.save` takes no OBS-side parameters (obs-websocket's `SaveReplayBuffer`
    has none); `reason` is caller-side context for logs/audit only, never sent to OBS."""

    reason: str = ""


#: An `obs.request` answer larger than this is cut: the model gets a preview, not megabytes.
MAX_RESPONSE_CHARS = 24_000
#: How many `obs.screenshot` files are kept; older ones are deleted.
SCREENSHOTS_KEPT = 5
#: How deep `obs.request` follows nested scenes and groups inside the privacy scene.
PRIVACY_SCENE_DEPTH = 8
#: Names Nox gives the sources it sets up (the user may rename them; the input is then created
#: again under this name on the next setup).
OVERLAY_INPUT = "Nox Overlay"
VOICE_INPUT = "Nox Voice"
#: What a virtual audio cable's name contains (VB-Audio Virtual Cable, VoiceMeeter, and the like).
VIRTUAL_DEVICE_WORDS = ("cable", "voicemeeter", "virtual")


class ObsRequestInput(BaseModel):
    """Input of `obs.request`: one obs-websocket v5 request. Batches are not offered - every
    request is classified and confirmed on its own."""

    request_type: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Z][A-Za-z0-9]+$")
    request_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("request_data")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, default=str)) > 64_000:
            raise ValueError("request_data is larger than 64 kB")
        return value


class ScreenshotInput(BaseModel):
    """`obs.screenshot`: a small local image of a source or scene (default: the program scene)."""

    source_name: str = Field(default="", max_length=200)
    width: int = Field(default=640, ge=64, le=1280)


class OverlaySetupInput(BaseModel):
    """`obs.overlay.setup`: create or update the browser source that shows Nox on stream. The URL
    must point at Nox's own loopback web server; it carries the overlay credential, so it is never
    logged."""

    url: str = Field(..., min_length=10, max_length=600)
    input_name: str = Field(default=OVERLAY_INPUT, min_length=1, max_length=100)
    scene_name: str = Field(default="", max_length=200)
    width: int = Field(default=400, ge=64, le=3840)
    height: int = Field(default=400, ge=64, le=2160)

    @field_validator("url")
    @classmethod
    def _loopback_pet_page(cls, value: str) -> str:
        parts = urlsplit(value)
        try:
            loopback = ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
        if parts.scheme != "http" or not loopback or not parts.path.startswith("/pet/"):
            raise ValueError("the overlay URL must be Nox's own http://127.0.0.1:<port>/pet/ page")
        return value


class AudioTrackSetupInput(BaseModel):
    """`obs.audio_track.setup`: an audio input capturing the device Nox speaks into (the output
    side of a virtual cable), so Nox's voice is its own source - and track - in OBS."""

    device_hint: str = Field(default="CABLE Output", min_length=1, max_length=200)
    input_name: str = Field(default=VOICE_INPUT, min_length=1, max_length=100)
    scene_name: str = Field(default="", max_length=200)
    #: OBS audio tracks (1-6) the source is routed to; empty keeps OBS's default (all tracks).
    tracks: list[int] = Field(default_factory=list, max_length=6)

    @field_validator("device_hint")
    @classmethod
    def _virtual_device(cls, value: str) -> str:
        # Only the virtual cable Nox speaks into - never a microphone or any other real device,
        # which would put the room on stream under Nox's name.
        if not any(word in value.casefold() for word in VIRTUAL_DEVICE_WORDS):
            raise ValueError("only a virtual audio cable can be Nox's track")
        return value

    @field_validator("tracks")
    @classmethod
    def _track_numbers(cls, value: list[int]) -> list[int]:
        if any(t < 1 or t > 6 for t in value):
            raise ValueError("OBS audio tracks are numbered 1 to 6")
        return sorted(set(value))


class ObsPlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        self._current_scene = ""
        self._streaming = False
        self._session_id = ""
        self._session_started_at = 0.0
        #: Futures waiting on the next `ReplayBufferSaved` event ( step 4-5): OBS's
        #: `SaveReplayBuffer` response carries no path, only the later event does.
        self._replay_waiters: list[asyncio.Future[str]] = []
        self._background: set[asyncio.Task[None]] = set()
        self._panicked = False
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
            on_auth_failed=self._on_obs_auth_failed,
        )
        configured_dir = str(api.config.get("screenshot_dir", "") or "")
        self._screenshot_dir = (
            Path(configured_dir) if configured_dir else Path(tempfile.gettempdir()) / "nox-obs"
        )

    def _url(self) -> str:
        host = str(self.api.config.get("host", "127.0.0.1"))
        port = int(self.api.config.get("port", 4455))
        return f"ws://{host}:{port}"

    def _authorize_egress(self, url: str) -> None:
        """Raw `websockets` connections bypass `PluginApi.http`'s guard, so this plugin enforces
        the scoped `EgressGuard` (manifest `network.egress` + privacy mode) itself before every
        connection attempt."""
        parts = urlsplit(url)
        self.api.egress.authorize(parts.hostname or "", parts.port or 4455, scheme="ws")

    async def _get_password(self) -> str | None:
        return await self.api.secrets.get(OBS_PASSWORD_SECRET)

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        self.api.events.on("security.panic", self._on_panic)
        self.client.start()

    async def stop(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.client.stop()

    # -- health --------------------------------------------------------------------------------

    async def health(self) -> tuple[HealthStatus, str]:
        if not (self.client.connected and self.client.identified):
            if await self._get_password() is None:
                return HealthStatus.UNAVAILABLE, "no secret configured: " + OBS_PASSWORD_SECRET
            if self.client.auth_failed:
                return HealthStatus.UNAVAILABLE, "OBS rejected the login: " + (
                    self.client.last_error or "authentication failed"
                )
            return HealthStatus.UNAVAILABLE, self.client.last_error or "not connected to OBS"
        if self.client.event_subscriptions == 0:
            return HealthStatus.LIMITED, "connected but not subscribed to OBS events"
        return HealthStatus.AVAILABLE, "connected"

    # -- OBS event handling ----------------------------------------------------------------------

    async def _on_obs_connected(self) -> None:
        await self.api.events.emit("obs.connected", {"reason": ""})
        # OBS may already be live when Nox (re)connects - after a Nox restart in the middle of a
        # broadcast, say. `StreamStateChanged` only reports changes, so ask once. As a task: this
        # hook runs before the client reads responses, so awaiting a request here would wait for
        # an answer nobody is reading yet.
        task = asyncio.create_task(self._sync_stream_state(), name="obs-stream-state")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _sync_stream_state(self) -> None:
        try:
            status = await self.client.request("GetStreamStatus")
        except (ConnectionError, ObsRequestError, TimeoutError) as exc:
            self.api.log.warning("obs.stream_status_unavailable", error=str(exc))
            return
        await self._on_stream_state(bool(status.get("outputActive", False)))

    async def _on_obs_auth_failed(self, reason: str) -> None:
        """Tell the core once per run of failures; it reports it to the user (health, notice)."""
        await self.api.events.emit("obs.auth_failed", {"reason": reason})

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
        """: switch to the privacy scene without the normal `confirm` step. Never
        touches the stream output itself - if OBS is unreachable this only logs and reports on the
        private channel via the plugin log; `stream.stop`/`recording.delete` are hard prohibitions
        and are never attempted, here or anywhere else in this plugin."""
        # From now on obs.request changes nothing, whatever the core decided: the panic scene
        # stays on until a person moves away from it in OBS (or Nox restarts the plugin).
        self._panicked = True
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
        """`obs.status.read`: full scene/source/stream-state snapshot."""
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
        """`obs.preflight.check`: draft of the pre-flight items this plugin can actually judge;
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

    # -- Clip Pipeline -----------------------------------------------

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
        returned as a structured `{"ok": False, "reason":...}` result rather than raised, so the
        caller (dashboard, `nox.clips` service) gets a clear, non-crashing message."""
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

    # -- generic request, screenshot, one-click setups -------------------------------------------

    def _require_connection(self) -> None:
        if not (self.client.connected and self.client.identified):
            raise ConnectionError(
                f"not connected to OBS ({self.client.last_error or 'unavailable'})"
            )

    def _privacy_scene(self) -> str:
        return str(self.api.config.get("privacy_scene", "") or "")

    async def request(self, data: ObsRequestInput) -> dict[str, Any]:
        """`obs.request`: any obs-websocket v5 request. The core has already classified and
        confirmed this call (`nox.stream.obs_requests`); the refusal check here is a second lock
        on the same door, in case this handler is ever reached another way."""
        refused = refusal(data.request_type)
        if refused is not None:
            raise PermissionError(refused)
        self._require_connection()
        scope = classify(data.request_type, data.request_data, privacy_scene=self._privacy_scene())
        if scope.risk is not Risk.READ:
            if self._panicked:
                raise PermissionError("the panic button was pressed; OBS stays as it is")
            await self._check_privacy_scene(data)
        try:
            raw = await self.client.request(data.request_type, data.request_data)
        except ObsRequestError as exc:
            # OBS's own explanation ("No source was found by the name of ...") is what lets the
            # model correct its next request, so it is a result, not a crash.
            return {
                "ok": False,
                "request_type": data.request_type,
                "code": exc.code,
                "comment": redact_text(exc.comment),
            }
        response = redact(raw)
        text = json.dumps(response, default=str)
        if len(text) > MAX_RESPONSE_CHARS:
            return {
                "ok": True,
                "request_type": data.request_type,
                "truncated": True,
                "response_preview": text[:MAX_RESPONSE_CHARS],
            }
        return {"ok": True, "request_type": data.request_type, "response": response}

    async def _check_privacy_scene(self, data: ObsRequestInput) -> None:
        """Refuse a change to the privacy scene or any source in it, by name or UUID. What the
        privacy scene holds is asked from OBS right now; if OBS cannot say, the request is
        refused - the panic path is worth more than one request."""
        privacy = self._privacy_scene()
        if not privacy:
            return
        try:
            scenes = await self.client.request("GetSceneList")
            entry = next(
                (s for s in scenes.get("scenes", []) if s.get("sceneName") == privacy), None
            )
            own = {privacy}
            protected: set[str] = set()
            if entry is not None:
                own.add(str(entry.get("sceneUuid", "")))
                await self._collect_contents(privacy, group=False, into=protected, depth=0)
        except (ObsRequestError, ConnectionError, TimeoutError, RecursionError) as exc:
            raise PermissionError(
                f"could not check the privacy scene in OBS ({exc}); nothing was changed"
            ) from exc
        reason = privacy_refusal(
            data.request_type, data.request_data, scene_names=own, protected=protected
        )
        if reason is not None:
            raise PermissionError(reason)

    async def _collect_contents(
        self, name: str, *, group: bool, into: set[str], depth: int
    ) -> None:
        """Every source in scene (or group) `name`, by name and UUID, descending into nested
        scenes and groups - a source two levels down is still on screen when the panic hits."""
        if depth > PRIVACY_SCENE_DEPTH:
            raise RecursionError("the privacy scene is nested too deeply to check")
        request = "GetGroupSceneItemList" if group else "GetSceneItemList"
        items = await self.client.request(request, {"sceneName": name})
        for item in items.get("sceneItems", []):
            source = str(item.get("sourceName", ""))
            if not source:
                continue
            already = source in into
            into.update({source, str(item.get("sourceUuid", ""))})
            if already:
                continue  # a scene nested twice, or a cycle
            if item.get("isGroup"):
                await self._collect_contents(source, group=True, into=into, depth=depth + 1)
            elif item.get("sourceType") == "OBS_SOURCE_TYPE_SCENE":
                await self._collect_contents(source, group=False, into=into, depth=depth + 1)

    async def screenshot(self, data: ScreenshotInput) -> dict[str, Any]:
        """`obs.screenshot`: a small JPEG of a source or scene, written to a local folder. Only
        the file name and size are returned - the image never travels with the tool result, so
        it never reaches a cloud model by accident."""
        self._require_connection()
        source = data.source_name or await self._current_scene_name()
        response = await self.client.request(
            "GetSourceScreenshot",
            {
                "sourceName": source,
                "imageFormat": "jpg",
                "imageWidth": data.width,
                "imageCompressionQuality": 70,
            },
        )
        image = _decode_image(str(response.get("imageData", "")))
        path = await asyncio.to_thread(self._store_screenshot, image)
        # The file name only: the folder's path names the user, and the model has no use for it.
        return {"source": source, "file": path.name, "bytes": len(image), "width": data.width}

    def _store_screenshot(self, image: bytes) -> Path:
        directory = self._screenshot_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"obs-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.jpg"
        path.write_bytes(image)
        older = sorted(directory.glob("obs-*.jpg"), key=lambda p: p.stat().st_mtime)
        for stale in older[:-SCREENSHOTS_KEPT]:
            stale.unlink(missing_ok=True)
        return path

    async def _current_scene_name(self) -> str:
        current = await self.client.request("GetCurrentProgramScene")
        scene = str(current.get("currentProgramSceneName", "") or current.get("sceneName", ""))
        if not scene:
            raise ValueError("OBS reported no current program scene")
        return scene

    async def _target_scene(self, requested: str) -> str:
        scene = requested or await self._current_scene_name()
        if scene == self._privacy_scene():
            raise PermissionError(
                f"{scene!r} is the privacy scene; switch to the scene you stream from first"
            )
        return scene

    async def _input_names(self, kind: str) -> set[str]:
        listed = await self.client.request("GetInputList", {"inputKind": kind})
        return {str(i.get("inputName", "")) for i in listed.get("inputs", [])}

    async def _ensure_in_scene(self, scene: str, input_name: str) -> bool:
        """Add an existing input to `scene` unless it is already there. True when added."""
        try:
            await self.client.request(
                "GetSceneItemId", {"sceneName": scene, "sourceName": input_name}
            )
        except ObsRequestError:
            await self.client.request(
                "CreateSceneItem",
                {"sceneName": scene, "sourceName": input_name, "sceneItemEnabled": True},
            )
            return True
        return False

    async def overlay_setup(self, data: OverlaySetupInput) -> dict[str, Any]:
        """`obs.overlay.setup`: the browser source that shows Nox on stream, created in the
        current scene or updated in place (same name), always with a transparent background."""
        self._require_connection()
        scene = await self._target_scene(data.scene_name)
        settings = {
            "url": data.url,
            "width": data.width,
            "height": data.height,
            "css": "body { background-color: rgba(0, 0, 0, 0); margin: 0; overflow: hidden; }",
            "reroute_audio": False,
            "shutdown": False,
        }
        if data.input_name in await self._input_names("browser_source"):
            await self.client.request(
                "SetInputSettings",
                {"inputName": data.input_name, "inputSettings": settings, "overlay": True},
            )
            added = await self._ensure_in_scene(scene, data.input_name)
            return {"input_name": data.input_name, "scene": scene, "created": False, "added": added}
        await self.client.request(
            "CreateInput",
            {
                "sceneName": scene,
                "inputName": data.input_name,
                "inputKind": "browser_source",
                "inputSettings": settings,
                "sceneItemEnabled": True,
            },
        )
        return {"input_name": data.input_name, "scene": scene, "created": True, "added": True}

    async def audio_track_setup(self, data: AudioTrackSetupInput) -> dict[str, Any]:
        """`obs.audio_track.setup`: an audio input on the device Nox speaks into. OBS lists the
        devices an input can capture only through an existing input, so a new one is created
        first and then pointed at the device; if no device matches, that new input is removed
        again rather than left behind half set up."""
        self._require_connection()
        scene = await self._target_scene(data.scene_name)
        created = data.input_name not in await self._input_names("wasapi_input_capture")
        if created:
            await self.client.request(
                "CreateInput",
                {
                    "sceneName": scene,
                    "inputName": data.input_name,
                    "inputKind": "wasapi_input_capture",
                    "inputSettings": {},
                    "sceneItemEnabled": True,
                },
            )
        items = await self.client.request(
            "GetInputPropertiesListPropertyItems",
            {"inputName": data.input_name, "propertyName": "device_id"},
        )
        devices = [
            (str(i.get("itemName", "")), str(i.get("itemValue", "")))
            for i in items.get("propertyItems", [])
            if i.get("itemEnabled", True)
        ]
        hint = data.device_hint.casefold()
        match = next(((n, v) for n, v in devices if hint in n.casefold() and v), None)
        if match is None:
            if created:
                await self.client.request("RemoveInput", {"inputName": data.input_name})
            return {
                "ok": False,
                "reason": f"no OBS audio input device matches {data.device_hint!r}",
                "devices": [n for n, _ in devices],
            }
        await self.client.request(
            "SetInputSettings",
            {"inputName": data.input_name, "inputSettings": {"device_id": match[1]}},
        )
        if data.tracks:
            await self.client.request(
                "SetInputAudioTracks",
                {
                    "inputName": data.input_name,
                    "inputAudioTracks": {str(t): t in data.tracks for t in range(1, 7)},
                },
            )
        added = created or await self._ensure_in_scene(scene, data.input_name)
        return {
            "ok": True,
            "input_name": data.input_name,
            "device": match[0],
            "scene": scene,
            "created": created,
            "added": added,
            "tracks": data.tracks,
        }


def _decode_image(data_url: str) -> bytes:
    """The bytes of OBS's `imageData` (a `data:image/jpg;base64,...` URL)."""
    _, _, encoded = data_url.partition("base64,")
    try:
        image = base64.b64decode(encoded or data_url, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("OBS returned no readable image") from exc
    if not image:
        raise ValueError("OBS returned an empty image")
    return image


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
        description="Pre-flight check for the items OBS itself can answer.",
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
    api.tools.register(
        "obs.request",
        ObsRequestInput,
        plugin.request,
        Risk.HIGH,
        description=TOOL_GUIDE,
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "obs.screenshot",
        ScreenshotInput,
        plugin.screenshot,
        Risk.READ,
        description=(
            "Small JPEG of an OBS source or scene (default: the program scene), saved to a local "
            "folder; returns its path and size, never the image itself. To check a layout "
            "numerically, use obs.request GetSceneItemTransform."
        ),
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "obs.overlay.setup",
        OverlaySetupInput,
        plugin.overlay_setup,
        Risk.MEDIUM,
        description=(
            "Create or update the 'Nox Overlay' browser source in the current scene. Needs the "
            "overlay URL with its credential, which only Nox's own setup has - call "
            "stream.overlay.setup instead."
        ),
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "obs.audio_track.setup",
        AudioTrackSetupInput,
        plugin.audio_track_setup,
        Risk.MEDIUM,
        description=(
            "Create the 'Nox Voice' audio input in OBS on the device Nox speaks into (default: "
            "the output side of VB-Audio Virtual Cable, 'CABLE Output'), optionally routed to "
            "OBS audio tracks 1-6 only."
        ),
        side_effects=True,
        local=True,
    )
    return plugin
