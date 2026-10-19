"""The stream-control services on their own, with a fake tool executor: the overlay setup and its
credential hand-over, the audio track, the automatic stream session, the Twitch live check and
the login notices."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from nox.core.events import E, Event, HealthStatus
from nox.core.state import Mode
from nox.ipc.overlay_tokens import OBS_LABEL, OverlayTokenStore
from nox.settings.twitch_auth import TwitchAuthState
from nox.stream.audio_track import AudioTrackRequest, AudioTrackService, find_cable
from nox.stream.autostart import AutoStreamSession, TwitchLiveWatcher
from nox.stream.notices import AuthWatch, CaptureWarning
from nox.stream.overlay import HIDDEN, OverlayService, OverlaySetupRequest, overlay_url
from nox.tools.executor import ToolResult
from tests.unit.fakes import FakeBus


class FakeExecutor:
    """Records tool calls; answers from `answers` (name -> ToolResult)."""

    def __init__(self, answers: dict[str, ToolResult] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.answers = answers or {}

    async def call(
        self, agent: str, name: str, arguments: dict[str, Any], **_kw: Any
    ) -> ToolResult:
        self.calls.append((agent, name, arguments))
        return self.answers.get(name, ToolResult(ok=True, data={}, decision="allow"))


class FakeHub:
    def __init__(self) -> None:
        self.disconnected: list[list[str]] = []

    async def disconnect_credential(self, ids: Any, reason: str = "") -> int:
        self.disconnected.append(list(ids))
        return len(list(ids))


DENIED = ToolResult(ok=False, error="permission.denied", decision="deny")


# ---- overlay -------------------------------------------------------------------------------


def make_overlay(
    tmp_path: Path, executor: FakeExecutor, hub: FakeHub
) -> tuple[OverlayService, OverlayTokenStore]:
    store = OverlayTokenStore(tmp_path / "overlay.json")
    service = OverlayService(
        store,
        executor,  # type: ignore[arg-type]
        http_port=lambda: 47801,
        variant=lambda: "neutral",
        mode=lambda: "stream",
        hub=hub,
    )
    return service, store


async def test_overlay_setup_hands_obs_a_url_that_authenticates(tmp_path: Path) -> None:
    executor = FakeExecutor(
        {"obs.overlay.setup": ToolResult(ok=True, data={"scene": "Live", "created": True})}
    )
    service, store = make_overlay(tmp_path, executor, FakeHub())
    result = await service.setup(OverlaySetupRequest(), agent="dashboard", origin="local")
    assert result["ok"] is True and result["scene"] == "Live"
    url = executor.calls[0][2]["url"]
    assert url.startswith("http://127.0.0.1:47801/pet/?overlay=1#overlay=")
    token = url.split("#overlay=", 1)[1]
    assert store.verify(token) is not None and store.verify(token).label == OBS_LABEL  # type: ignore[union-attr]
    # The token never comes back in the result - the model would see it.
    assert token not in str(result) and HIDDEN in result["url"]


async def test_a_declined_setup_keeps_the_old_overlay_working(tmp_path: Path) -> None:
    hub = FakeHub()
    executor = FakeExecutor({"obs.overlay.setup": ToolResult(ok=True, data={})})
    service, store = make_overlay(tmp_path, executor, hub)
    await service.setup(OverlaySetupRequest(), agent="dashboard", origin="local")
    old_token = executor.calls[0][2]["url"].split("#overlay=", 1)[1]
    executor.answers["obs.overlay.setup"] = DENIED
    result = await service.setup(OverlaySetupRequest(), agent="dashboard", origin="local")
    assert result["ok"] is False
    new_token = executor.calls[1][2]["url"].split("#overlay=", 1)[1]
    assert store.verify(old_token) is not None
    assert store.verify(new_token) is None
    assert hub.disconnected == []


async def test_a_new_setup_replaces_and_disconnects_the_old_overlay(tmp_path: Path) -> None:
    hub = FakeHub()
    executor = FakeExecutor({"obs.overlay.setup": ToolResult(ok=True, data={})})
    service, store = make_overlay(tmp_path, executor, hub)
    await service.setup(OverlaySetupRequest(), agent="dashboard", origin="local")
    old_id = store.credentials()[0].credential_id
    await service.setup(OverlaySetupRequest(), agent="dashboard", origin="local")
    assert hub.disconnected == [[old_id]]
    assert len(store.credentials()) == 1


async def test_revoke_locks_every_overlay_out(tmp_path: Path) -> None:
    hub = FakeHub()
    executor = FakeExecutor({"obs.overlay.setup": ToolResult(ok=True, data={})})
    service, store = make_overlay(tmp_path, executor, hub)
    await service.setup(OverlaySetupRequest(), agent="dashboard", origin="local")
    result = await service.revoke()
    assert result["revoked"] == 1 and store.credentials() == []
    assert service.status()["configured"] is False


def test_overlay_url_shape() -> None:
    assert overlay_url(47801, "t") == "http://127.0.0.1:47801/pet/?overlay=1#overlay=t"
    assert overlay_url(47801, "t", variant="kitsune").endswith(
        "?overlay=1&variant=kitsune#overlay=t"
    )


# ---- audio track ---------------------------------------------------------------------------


class FakeEditor:
    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []

    async def apply(self, values: Any, *, by: str = "", pin: str | None = None) -> dict[str, Any]:
        self.applied.append(dict(values))
        return {"ok": True}


def make_audio(executor: FakeExecutor, editor: FakeEditor, devices: list[str] | None) -> Any:
    async def list_devices() -> list[str] | None:
        return devices

    return AudioTrackService(
        executor,  # type: ignore[arg-type]
        devices=list_devices,
        editor=lambda: editor,
        mode=lambda: "stream",
    )


CABLE = "CABLE Input (VB-Audio Virtual Cable)"


def test_find_cable() -> None:
    assert find_cable(["Speakers", CABLE]) == CABLE
    assert find_cable(["Speakers", "Headphones"]) is None


async def test_audio_track_sets_up_obs_then_routes_nox_to_the_cable() -> None:
    executor = FakeExecutor(
        {"obs.audio_track.setup": ToolResult(ok=True, data={"ok": True, "input_name": "Nox Voice"})}
    )
    editor = FakeEditor()
    service = make_audio(executor, editor, ["Headphones", CABLE])
    result = await service.setup(AudioTrackRequest(tracks=[3]), agent="dashboard", origin="local")
    assert result["ok"] is True and result["nox_plays_to"] == CABLE
    assert executor.calls[0][2]["device_hint"] == "CABLE Output"
    assert executor.calls[0][2]["tracks"] == [3]
    assert editor.applied == [
        {"voice.channels.stream_device": CABLE, "voice.channels.routing": "both"}
    ]


async def test_declined_obs_step_leaves_the_voice_settings_alone() -> None:
    editor = FakeEditor()
    service = make_audio(FakeExecutor({"obs.audio_track.setup": DENIED}), editor, [CABLE])
    result = await service.setup(AudioTrackRequest(), agent="dashboard", origin="local")
    assert result["ok"] is False and editor.applied == []


async def test_without_a_virtual_cable_nothing_is_asked() -> None:
    executor = FakeExecutor()
    service = make_audio(executor, FakeEditor(), ["Speakers"])
    result = await service.setup(AudioTrackRequest(), agent="dashboard", origin="local")
    assert result["ok"] is False and "VB-Audio" in result["reason"]
    assert executor.calls == []


# ---- automatic stream session --------------------------------------------------------------


class ModeBox:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.switches: list[tuple[Mode, str]] = []
        self.notices: list[str] = []

    async def switch(self, mode: Mode, by: str) -> None:
        self.switches.append((mode, by))
        self.mode = mode.value

    async def notify(self, current: str) -> None:
        self.notices.append(current)


def make_auto(bus: FakeBus, box: ModeBox) -> AutoStreamSession:
    auto = AutoStreamSession(
        bus,  # type: ignore[arg-type]
        current_mode=lambda: box.mode,
        switch_mode=box.switch,
        notify=box.notify,
        from_modes=["companion", "idle"],
    )
    auto.start()
    return auto


async def test_obs_going_live_starts_stream_mode_and_ending_returns() -> None:
    bus, box = FakeBus(), ModeBox("companion")
    make_auto(bus, box)
    await bus.publish(Event(name=E.STREAM_STARTED, payload={"session_id": "s"}))
    assert box.switches == [(Mode.STREAM, "stream.live.obs")]
    await bus.publish(Event(name=E.STREAM_ENDED, payload={"session_id": "s"}))
    assert box.switches[-1] == (Mode.COMPANION, "stream.ended")


async def test_a_busy_mode_is_not_taken_over() -> None:
    bus, box = FakeBus(), ModeBox("coding")
    make_auto(bus, box)
    await bus.publish(Event(name=E.STREAM_STARTED, payload={}))
    assert box.switches == [] and box.notices == ["coding"]


async def test_a_stream_mode_chosen_by_the_user_is_left_alone_at_the_end() -> None:
    bus, box = FakeBus(), ModeBox("stream")
    make_auto(bus, box)
    await bus.publish(Event(name=E.STREAM_STARTED, payload={}))
    await bus.publish(Event(name=E.STREAM_ENDED, payload={}))
    assert box.switches == []


class HelixTransport(httpx.AsyncBaseTransport):
    def __init__(self, live: bool) -> None:
        self.live = live
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        data = [{"type": "live", "user_login": "chan"}] if self.live else []
        return httpx.Response(200, json={"data": data})


def make_watcher(
    transport: HelixTransport, mode: list[str], lives: list[int], creds: Any = ("cid", "tok")
) -> TwitchLiveWatcher:
    async def on_live() -> None:
        lives.append(1)

    return TwitchLiveWatcher(
        channel=lambda: "#Chan",
        credentials=lambda: creds,
        client_factory=lambda **kw: httpx.AsyncClient(transport=transport, **kw),
        current_mode=lambda: mode[0],
        on_live=on_live,
        interval_s=60.0,
    )


async def test_twitch_live_check_fires_once_per_live_period() -> None:
    transport, mode, lives = HelixTransport(live=True), ["companion"], []
    watcher = make_watcher(transport, mode, lives)
    assert await watcher.check_once() is True
    assert await watcher.check_once() is True
    assert lives == [1]
    request = transport.requests[0]
    assert request.url.params["user_login"] == "chan"
    assert request.headers["Client-Id"] == "cid"
    assert request.headers["Authorization"] == "Bearer tok"


async def test_twitch_live_check_is_silent_while_streaming_or_signed_out() -> None:
    transport, lives = HelixTransport(live=True), []
    assert await make_watcher(transport, ["stream"], lives).check_once() is None
    assert await make_watcher(transport, ["companion"], lives, creds=None).check_once() is None
    assert transport.requests == [] and lives == []


# ---- notices -------------------------------------------------------------------------------


class FakeTwitch:
    def __init__(self, state: TwitchAuthState) -> None:
        self.state = state
        self.started = 0

    @property
    def needs_login(self) -> bool:
        return self.state is TwitchAuthState.EXPIRED

    def status(self) -> dict[str, Any]:
        return {"state": self.state.value}

    async def start(self) -> dict[str, Any]:
        self.started += 1
        return {"user_code": "ABCD-1234", "verification_uri": "https://www.twitch.tv/activate"}


def make_watch(twitch: FakeTwitch, notices: list[tuple[str, str]], clock: list[float]) -> AuthWatch:
    async def notify(key: str, text: str) -> None:
        notices.append((key, text))

    watch = AuthWatch(
        FakeBus(),  # type: ignore[arg-type]
        twitch,  # type: ignore[arg-type]
        notify=notify,
        language=lambda: "en",
        twitch_enabled=lambda: True,
        clock=lambda: clock[0],
    )
    return watch


async def test_expired_twitch_login_starts_the_device_flow_and_shows_the_code() -> None:
    twitch, notices, clock = FakeTwitch(TwitchAuthState.EXPIRED), [], [0.0]
    watch = make_watch(twitch, notices, clock)
    await watch._on_twitch_auth(Event(name=E.TWITCH_AUTH_CHANGED, payload={"state": "expired"}))
    assert twitch.started == 1
    assert notices == [
        (
            "twitch.auth",
            "Twitch login expired - open https://www.twitch.tv/activate and enter the code "
            "ABCD-1234.",
        )
    ]
    # Not again within the hour's budget...
    await watch._on_twitch_auth(Event(name=E.TWITCH_AUTH_CHANGED, payload={"state": "expired"}))
    assert twitch.started == 1
    # ...but again later, e.g. when the next stream starts.
    clock[0] = 3600.0
    await watch._on_stream_started(Event(name=E.STREAM_STARTED, payload={}))
    assert twitch.started == 2
    status, reason = await watch.twitch_health()
    assert status is HealthStatus.UNAVAILABLE and "expired" in reason


async def test_a_working_login_starts_nothing() -> None:
    twitch, notices, clock = FakeTwitch(TwitchAuthState.AUTHORIZED), [], [0.0]
    watch = make_watch(twitch, notices, clock)
    await watch._on_stream_started(Event(name=E.STREAM_STARTED, payload={}))
    assert twitch.started == 0 and notices == []
    assert (await watch.twitch_health())[0] is HealthStatus.AVAILABLE


async def test_obs_login_failure_is_health_and_one_notice() -> None:
    notices: list[tuple[str, str]] = []
    watch = make_watch(FakeTwitch(TwitchAuthState.AUTHORIZED), notices, [0.0])
    failed = Event(name=E.OBS_AUTH_FAILED, payload={"reason": "wrong password"})
    await watch._on_obs_auth_failed(failed)
    await watch._on_obs_auth_failed(failed)
    assert [k for k, _ in notices] == ["obs.auth"]
    assert (await watch.obs_health())[0] is HealthStatus.UNAVAILABLE
    await watch._on_obs_connected(Event(name=E.OBS_CONNECTED, payload={}))
    assert (await watch.obs_health())[0] is HealthStatus.AVAILABLE


async def test_capture_warning_when_a_monitor_capture_is_live_and_the_pet_is_open() -> None:
    answers = {
        "obs.request": ToolResult(ok=True, data={"ok": True, "response": {}}),
    }
    executor = FakeExecutor(answers)
    responses = iter(
        [
            {"inputs": [{"inputName": "Display"}]},
            {"videoActive": True, "videoShowing": True},
        ]
    )

    async def call(agent: str, name: str, arguments: dict[str, Any], **_kw: Any) -> ToolResult:
        executor.calls.append((agent, name, arguments))
        return ToolResult(ok=True, data={"ok": True, "response": next(responses)})

    executor.call = call  # type: ignore[method-assign]
    notices: list[tuple[str, str]] = []

    async def notify(key: str, text: str) -> None:
        notices.append((key, text))

    warning = CaptureWarning(
        FakeBus(),  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
        pet_open=lambda: True,
        notify=notify,
        language=lambda: "en",
        mode=lambda: "stream",
    )
    assert await warning.check() is True
    assert await warning.check() is False  # once per stream
    assert [c[2]["request_type"] for c in executor.calls] == ["GetInputList", "GetSourceActive"]
    assert notices[0][0] == "stream.pet_on_capture"


async def test_no_capture_warning_without_the_desktop_pet() -> None:
    executor = FakeExecutor()
    warning = CaptureWarning(
        FakeBus(),  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
        pet_open=lambda: False,
        notify=lambda *_: asyncio.sleep(0),
        language=lambda: "en",
        mode=lambda: "stream",
    )
    assert await warning.check() is False and executor.calls == []
