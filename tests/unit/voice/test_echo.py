"""Echo protection: what kind of output, and the stricter start rule while Nox speaks.

No sound device and no registry value is read here: the probes are driven with fakes, the
platform readers with a fake `winreg` and fake command output.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from nox.core.events import E
from nox.voice import echo
from nox.voice.base import TtsRequest
from nox.voice.echo import (
    EchoGuard,
    EchoGuardedVad,
    EchoMode,
    EchoParams,
    Endpoint,
    MacProbe,
    NameProbe,
    OutputKind,
    PactlProbe,
    WindowsEndpointProbe,
    classify_name,
    kind_from_form_factor,
)
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.voice.vad import EnergyVad, Segmenter, frame_rms_db
from tests.unit.voice import audio_fixtures as fx
from tests.unit.voice.conftest import FakeAudioInput, FakeAudioOutput, FakeStt, FakeTts, settle

FRAME = fx.SR * 30 // 1000


def const_frame(db: float, n: int = FRAME) -> np.ndarray:
    """A frame whose RMS level is `db` dBFS."""
    return np.full(n, 10 ** (db / 20), dtype=np.float32)


class LevelVad:
    """Speech = anything louder than -70 dBFS; counts the frames it was shown."""

    def __init__(self) -> None:
        self.seen = 0
        self.resets = 0

    def is_speech(self, frame: np.ndarray) -> bool:
        self.seen += 1
        return frame_rms_db(frame) > -70.0

    def reset(self) -> None:
        self.resets += 1


class FakeProbe:
    def __init__(self, kind: OutputKind = OutputKind.LOUDSPEAKER) -> None:
        self.answer = kind
        self.asked: list[str] = []
        self.fail: Exception | None = None

    def kind(self, device: str) -> OutputKind:
        self.asked.append(device)
        if self.fail is not None:
            raise self.fail
        return self.answer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_guard(
    kind: OutputKind = OutputKind.LOUDSPEAKER,
    *,
    mode: EchoMode = EchoMode.AUTO,
    speaking: list[bool] | None = None,
    **params: Any,
) -> tuple[EchoGuard, dict[str, bool]]:
    state = {"speaking": False}
    guard = EchoGuard(
        params=EchoParams(mode=mode, **params),
        probe=FakeProbe(kind),
        speaking=lambda: state["speaking"],
    )
    guard.detect()
    return guard, state


def guarded(
    kind: OutputKind = OutputKind.LOUDSPEAKER, *, mode: EchoMode = EchoMode.AUTO, **params: Any
) -> tuple[EchoGuardedVad, LevelVad, dict[str, bool]]:
    guard, state = make_guard(kind, mode=mode, **params)
    inner = LevelVad()
    return EchoGuardedVad(inner, guard), inner, state


# ---- what a device name says --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Speakers (Example Audio)", OutputKind.LOUDSPEAKER),
        ("Lautsprecher (Beispiel Audio)", OutputKind.LOUDSPEAKER),
        ("Haut-parleurs (Exemple)", OutputKind.LOUDSPEAKER),
        ("Altavoces (Ejemplo)", OutputKind.LOUDSPEAKER),
        ("Soundbar", OutputKind.LOUDSPEAKER),
        ("Headphones (Example USB)", OutputKind.HEADPHONES),
        ("Kopfhörer (Beispiel)", OutputKind.HEADPHONES),
        ("Example Headset Earphone", OutputKind.HEADPHONES),
        ("Buds Hands-Free AG Audio", OutputKind.HEADPHONES),
        ("Example AirPods", OutputKind.HEADPHONES),
        ("Headset Speaker", OutputKind.HEADPHONES),  # headphone words win
        ("Virtual Cable Input", OutputKind.SILENT),
        ("Loopback Device", OutputKind.SILENT),
        ("HDMI Output", OutputKind.UNKNOWN),
        ("", OutputKind.UNKNOWN),
    ],
)
def test_a_device_name_is_classified_by_its_words(name: str, expected: OutputKind) -> None:
    assert classify_name(name) is expected


@pytest.mark.parametrize(
    ("form_factor", "name", "expected"),
    [
        (1, "Speakers", OutputKind.LOUDSPEAKER),
        (3, "Headphones", OutputKind.HEADPHONES),
        (5, "Headset", OutputKind.HEADPHONES),
        (6, "Handset", OutputKind.HEADPHONES),
        (9, "Monitor", OutputKind.UNKNOWN),
        (0, "Remote", OutputKind.UNKNOWN),
        (2, "Line", OutputKind.UNKNOWN),
        (4, "Microphone", OutputKind.UNKNOWN),
        (7, "Passthrough", OutputKind.UNKNOWN),
        (8, "S/PDIF", OutputKind.UNKNOWN),
        (10, "Other", OutputKind.UNKNOWN),
        (99, "Future", OutputKind.UNKNOWN),
        (1, "Headphones", OutputKind.HEADPHONES),  # jack sensing renamed the endpoint only
        (1, "Kopfhörer", OutputKind.HEADPHONES),
        (3, "Speakers", OutputKind.HEADPHONES),  # a headphone factor is never overruled
        (1, "Virtual Cable", OutputKind.SILENT),
    ],
)
def test_the_windows_form_factor_decides_unless_the_name_says_otherwise(
    form_factor: int, name: str, expected: OutputKind
) -> None:
    assert kind_from_form_factor(form_factor, name) is expected


def test_the_name_probe_resolves_the_setting_first() -> None:
    assert NameProbe().kind("Beispiel Kopfhörer") is OutputKind.HEADPHONES
    probe = NameProbe(resolve=lambda setting: f"Speakers for {setting}")
    assert probe.kind("desk") is OutputKind.LOUDSPEAKER


# ---- Windows ----------------------------------------------------------------------------------


def endpoint(name: str, adapter: str, form_factor: int, *, active: bool = True) -> Endpoint:
    return Endpoint(name=name, adapter=adapter, form_factor=form_factor, active=active)


def windows_probe(endpoints: list[Endpoint], resolved: str) -> WindowsEndpointProbe:
    return WindowsEndpointProbe(lambda: endpoints, lambda _setting: resolved)


def test_a_device_is_matched_to_its_endpoint_by_the_full_friendly_name() -> None:
    probe = windows_probe(
        [
            endpoint("Speakers", "Example Audio", 1),
            endpoint("Headphones", "Example Audio", 3),
        ],
        "Headphones (Example Audio)",
    )
    assert probe.kind("") is OutputKind.HEADPHONES


def test_the_match_ignores_case_and_inactive_endpoints() -> None:
    probe = windows_probe(
        [
            endpoint("Speakers", "Example Audio", 1, active=False),
            endpoint("Headphones", "Example Audio", 3),
        ],
        "SPEAKERS (example audio)",
    )
    assert probe.kind("") is OutputKind.LOUDSPEAKER  # inactive one skipped: nothing matches


def test_an_mme_name_cut_to_31_characters_still_matches() -> None:
    full = "Headphones (Example Wireless Audio Device)"
    probe = windows_probe([endpoint("Headphones", "Example Wireless Audio Device", 3)], full[:31])
    assert len(full[:31]) == 31
    assert probe.kind("") is OutputKind.HEADPHONES


def test_a_short_name_is_never_matched_as_a_prefix() -> None:
    # "Speakers" is the start of this endpoint's name, but not a cut-off MME name: no match, so
    # the words of the name decide (and they say loudspeakers, whatever that endpoint is).
    probe = windows_probe([endpoint("Speakers", "Example Audio", 3)], "Speakers")
    assert probe.kind("") is OutputKind.LOUDSPEAKER


def test_two_endpoints_with_one_name_and_different_kinds_cannot_be_told_apart() -> None:
    probe = windows_probe(
        [endpoint("Output", "Example Audio", 1), endpoint("Output", "Example Audio", 3)],
        "Output (Example Audio)",
    )
    assert probe.kind("") is OutputKind.UNKNOWN


def test_two_endpoints_with_one_name_and_one_kind_are_fine() -> None:
    probe = windows_probe(
        [endpoint("Speakers", "Example Audio", 1), endpoint("Speakers", "Example Audio", 1)],
        "Speakers (Example Audio)",
    )
    assert probe.kind("") is OutputKind.LOUDSPEAKER


def test_an_unresolvable_device_is_unknown() -> None:
    assert windows_probe([endpoint("Speakers", "A", 1)], "").kind("x") is OutputKind.UNKNOWN


def test_no_matching_endpoint_falls_back_to_the_words_in_the_name() -> None:
    probe = windows_probe([endpoint("Speakers", "Other", 1)], "Kopfhörer (Beispiel)")
    assert probe.kind("") is OutputKind.HEADPHONES


class FakeRegistry:
    """The few `winreg` calls `read_windows_endpoints` makes, over a dict of endpoints."""

    HKEY_LOCAL_MACHINE = object()

    def __init__(self, tree: dict[str, dict[str, Any]], properties: dict[str, dict[str, Any]]):
        self.tree = tree
        self.properties = properties

    class _Key:
        def __init__(self, values: dict[str, Any]) -> None:
            self.values = values

        def __enter__(self) -> FakeRegistry._Key:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def OpenKey(self, root: Any, path: str) -> Any:  # noqa: N802 - mirrors winreg
        if root is self.HKEY_LOCAL_MACHINE:
            return self._Key({})
        if path in self.tree:
            return self._Key(self.tree[path])
        guid = path.removesuffix(r"\Properties")
        if path.endswith(r"\Properties") and guid in self.properties:
            return self._Key(self.properties[guid])
        raise FileNotFoundError(path)

    def EnumKey(self, key: Any, index: int) -> str:  # noqa: N802
        names = sorted(self.tree)
        if index >= len(names):
            raise OSError("no more")
        return names[index]

    def QueryValueEx(self, key: Any, name: str) -> tuple[Any, int]:  # noqa: N802
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], 0


def registry(monkeypatch: pytest.MonkeyPatch, fake: FakeRegistry) -> None:
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")


FF = echo._PKEY_FORM_FACTOR
NAME = echo._PKEY_ENDPOINT_NAME
ADAPTER = echo._PKEY_ADAPTER_NAME


def test_the_registry_reader_builds_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRegistry(
        tree={"{a}": {"DeviceState": 1}, "{b}": {"DeviceState": 4}},
        properties={
            "{a}": {FF: 3, NAME: "Headphones", ADAPTER: "Example Audio"},
            "{b}": {FF: 1, NAME: "Speakers", ADAPTER: "Example Audio"},
        },
    )
    registry(monkeypatch, fake)

    found = echo.read_windows_endpoints()

    assert found == [
        Endpoint("Headphones", "Example Audio", 3, active=True),
        Endpoint("Speakers", "Example Audio", 1, active=False),  # state 4: not plugged in
    ]
    assert found[0].friendly_name == "Headphones (Example Audio)"


def test_an_endpoint_without_an_adapter_name_keeps_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRegistry(
        tree={"{a}": {"DeviceState": 1}},
        properties={"{a}": {FF: 1, NAME: "Speakers"}},
    )
    registry(monkeypatch, fake)

    [only] = echo.read_windows_endpoints()

    assert (only.adapter, only.friendly_name) == ("", "Speakers")


def test_an_unreadable_or_odd_endpoint_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRegistry(
        tree={"{a}": {"DeviceState": 1}, "{b}": {"DeviceState": 1}, "{c}": {"DeviceState": 1}},
        properties={
            "{a}": {NAME: "No form factor"},
            "{b}": {FF: "three", NAME: "Wrong type"},
            "{c}": {FF: 1, NAME: "Fine", ADAPTER: "X"},
        },
    )
    registry(monkeypatch, fake)

    assert [e.name for e in echo.read_windows_endpoints()] == ["Fine"]


def test_the_registry_reader_gives_nothing_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert echo.read_windows_endpoints() == []


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows registry")
def test_the_real_registry_can_be_read_without_error() -> None:
    for item in echo.read_windows_endpoints():
        assert isinstance(item.form_factor, int)
        assert item.name


# ---- resolving the device name ------------------------------------------------------------------


def fake_sounddevice(
    monkeypatch: pytest.MonkeyPatch, *, wasapi_default: int = 2, default: int | None = 0
) -> None:
    devices = [
        {"name": "Mapper", "max_output_channels": 2, "max_input_channels": 0, "hostapi": 0},
        {"name": "Wired Speakers", "max_output_channels": 2, "max_input_channels": 0, "hostapi": 0},
        {
            "name": "Speakers (Example Audio)",
            "max_output_channels": 2,
            "max_input_channels": 0,
            "hostapi": 1,
        },
    ]
    module = types.SimpleNamespace(
        query_devices=lambda index=None: devices if index is None else devices[index],
        query_hostapis=lambda: [
            {"name": "MME", "default_output_device": 0},
            {"name": "Windows WASAPI", "default_output_device": wasapi_default},
        ],
        default=types.SimpleNamespace(device=(None, default)),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", module)


def test_the_default_output_is_the_wasapi_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sounddevice(monkeypatch)
    assert echo.resolve_output_name("") == "Speakers (Example Audio)"


def test_without_a_wasapi_default_the_portaudio_default_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sounddevice(monkeypatch, wasapi_default=-1, default=1)
    assert echo.resolve_output_name("") == "Wired Speakers"


def test_a_named_output_resolves_to_its_full_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sounddevice(monkeypatch)
    assert echo.resolve_output_name("wired") == "Wired Speakers"


def test_an_output_that_cannot_be_resolved_gives_an_empty_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sounddevice(monkeypatch)
    assert echo.resolve_output_name("no such device") == ""
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # not installed
    assert echo.resolve_output_name("") == ""


# ---- Linux and macOS -------------------------------------------------------------------------


def sink(
    name: str, description: str, *, port: str = "", port_type: str = "", form_factor: str = ""
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "active_port": port,
        "ports": [{"name": port, "type": port_type}] if port else [],
        "properties": {"device.form_factor": form_factor} if form_factor else {},
    }


def pactl(sinks: list[dict[str, Any]], default: str = "") -> Callable[[list[str]], str]:
    def run(argv: list[str]) -> str:
        return json.dumps(sinks) if "sinks" in argv else default

    return run


def test_pulseaudio_reports_the_active_port_of_the_default_sink() -> None:
    sinks = [
        sink("alsa_a", "Built-in Audio", port="analog-output-headphones", port_type="Headphones"),
        sink("alsa_b", "Other", port="analog-output-speaker", port_type="Speaker"),
    ]
    assert PactlProbe(pactl(sinks, "alsa_a")).kind("") is OutputKind.HEADPHONES
    assert PactlProbe(pactl(sinks, "alsa_b")).kind("") is OutputKind.LOUDSPEAKER


def test_pulseaudio_prefers_the_form_factor_property() -> None:
    sinks = [sink("bluez_a", "Example Buds", form_factor="headset")]
    assert PactlProbe(pactl(sinks)).kind("") is OutputKind.HEADPHONES
    sinks = [sink("bluez_b", "Example Box", form_factor="speaker")]
    assert PactlProbe(pactl(sinks)).kind("") is OutputKind.LOUDSPEAKER


def test_pulseaudio_picks_the_sink_the_setting_names() -> None:
    sinks = [
        sink("alsa_a", "Desk Speakers", form_factor="speaker"),
        sink("alsa_b", "USB Headset", form_factor="headset"),
    ]
    probe = PactlProbe(pactl(sinks, "alsa_a"))
    assert probe.kind("usb headset") is OutputKind.HEADPHONES
    assert probe.kind("") is OutputKind.LOUDSPEAKER


def test_pulseaudio_without_a_clear_sink_falls_back_to_the_name() -> None:
    two = [sink("a", "One"), sink("b", "Two")]
    assert PactlProbe(pactl(two)).kind("Kopfhörer") is OutputKind.HEADPHONES  # no default sink
    assert PactlProbe(lambda _argv: "").kind("Soundbar") is OutputKind.LOUDSPEAKER  # no pactl
    assert PactlProbe(lambda _argv: "not json").kind("Headset") is OutputKind.HEADPHONES


def test_pulseaudio_uses_the_description_words_without_other_hints() -> None:
    assert PactlProbe(pactl([sink("a", "USB Headphones")])).kind("") is OutputKind.HEADPHONES
    assert PactlProbe(pactl([sink("a", "Mystery Box")])).kind("") is OutputKind.UNKNOWN


def mac_profile(*items: dict[str, str]) -> Callable[[list[str]], str]:
    return lambda _argv: json.dumps({"SPAudioDataType": [{"_items": list(items)}]})


def mac_device(name: str, transport: str, *, default: bool = False) -> dict[str, str]:
    item = {"_name": name, "coreaudio_device_transport": transport}
    if default:
        item["coreaudio_default_audio_output_device"] = "spaudio_yes"
    return item


def test_macos_reads_the_default_output() -> None:
    run = mac_profile(
        mac_device("Example Laptop Speakers", "coreaudio_device_type_builtin", default=True),
        mac_device("Example Buds", "coreaudio_device_type_bluetooth"),
    )
    assert MacProbe(run).kind("") is OutputKind.LOUDSPEAKER


def test_macos_decides_by_name_then_by_transport() -> None:
    builtin = "coreaudio_device_type_builtin"
    bluetooth = "coreaudio_device_type_bluetooth"
    assert (
        MacProbe(mac_profile(mac_device("External Headphones", builtin, default=True))).kind("")
        is OutputKind.HEADPHONES
    )
    assert (
        MacProbe(mac_profile(mac_device("Example Gadget", bluetooth, default=True))).kind("")
        is OutputKind.HEADPHONES
    )
    assert (
        MacProbe(
            mac_profile(mac_device("Example Gadget", "coreaudio_device_type_usb", default=True))
        ).kind("")
        is OutputKind.UNKNOWN
    )


def test_macos_looks_up_a_named_device_and_falls_back_to_the_words() -> None:
    run = mac_profile(
        mac_device("Desk Speakers", "x", default=True), mac_device("Example Headset", "x")
    )
    assert MacProbe(run).kind("headset") is OutputKind.HEADPHONES
    assert MacProbe(run).kind("kopfhörer") is OutputKind.HEADPHONES  # no such device: the name
    assert MacProbe(lambda _argv: "").kind("Speakers") is OutputKind.LOUDSPEAKER
    assert MacProbe(lambda _argv: "[]").kind("") is OutputKind.UNKNOWN


def test_a_command_that_cannot_run_gives_an_empty_answer() -> None:
    assert echo.run_command(["nox-no-such-command-anywhere"]) == ""
    assert echo.run_command([sys.executable, "-c", "print('hi')"]).strip() == "hi"
    assert echo.run_command([sys.executable, "-c", "import sys; sys.exit(3)"]) == ""


@pytest.mark.parametrize(
    ("platform", "name"),
    [
        ("win32", "WindowsEndpointProbe"),
        ("darwin", "MacProbe"),
        ("linux", "PactlProbe"),
        ("freebsd13", "NameProbe"),
    ],
)
def test_every_platform_gets_a_probe(
    monkeypatch: pytest.MonkeyPatch, platform: str, name: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    assert type(echo.default_probe()).__name__ == name


# ---- the guard ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (OutputKind.HEADPHONES, EchoMode.OFF),
        (OutputKind.SILENT, EchoMode.OFF),
        (OutputKind.LOUDSPEAKER, EchoMode.THRESHOLD),
        (OutputKind.UNKNOWN, EchoMode.THRESHOLD),
    ],
)
def test_auto_resolves_by_the_kind_of_output(kind: OutputKind, expected: EchoMode) -> None:
    guard, _ = make_guard(kind)
    assert guard.policy is expected


@pytest.mark.parametrize("mode", [EchoMode.OFF, EchoMode.THRESHOLD, EchoMode.HALF_DUPLEX])
@pytest.mark.parametrize("kind", list(OutputKind))
def test_a_chosen_mode_applies_whatever_the_output(kind: OutputKind, mode: EchoMode) -> None:
    guard, _ = make_guard(kind, mode=mode)
    assert guard.policy is mode


def test_before_the_first_look_the_output_counts_as_unknown() -> None:
    guard = EchoGuard(probe=FakeProbe(OutputKind.HEADPHONES))
    assert guard.kind is OutputKind.UNKNOWN
    assert guard.policy is EchoMode.THRESHOLD  # the cautious side until it is known


def test_detection_asks_the_probe_for_the_configured_device() -> None:
    probe = FakeProbe(OutputKind.HEADPHONES)
    guard = EchoGuard(probe=probe, device="desk")
    assert guard.detect() is OutputKind.HEADPHONES
    assert probe.asked == ["desk"]


def test_a_probe_that_fails_makes_the_output_unknown() -> None:
    probe = FakeProbe()
    probe.fail = OSError("registry unavailable")
    guard = EchoGuard(probe=probe)
    assert guard.detect() is OutputKind.UNKNOWN


def test_output_that_is_not_played_here_is_silent_and_never_probed() -> None:
    probe = FakeProbe(OutputKind.LOUDSPEAKER)
    guard = EchoGuard(probe=probe, listens_here=False)
    assert guard.kind is OutputKind.SILENT
    assert guard.detect() is OutputKind.SILENT
    assert probe.asked == []
    assert guard.policy is EchoMode.OFF


@pytest.mark.parametrize(
    ("kind", "words"),
    [
        (OutputKind.LOUDSPEAKER, "loudspeakers"),
        (OutputKind.UNKNOWN, "unidentified output"),
    ],
)
def test_loudspeakers_and_unknown_outputs_get_the_headphones_hint(
    kind: OutputKind, words: str
) -> None:
    guard, _ = make_guard(kind)
    assert guard.hint().startswith("headphones recommended")
    assert words in guard.hint()


@pytest.mark.parametrize("kind", [OutputKind.HEADPHONES, OutputKind.SILENT])
def test_headphones_and_silent_outputs_need_no_hint(kind: OutputKind) -> None:
    assert make_guard(kind)[0].hint() == ""


def test_an_owner_who_switched_the_rule_off_gets_no_hint() -> None:
    assert make_guard(OutputKind.LOUDSPEAKER, mode=EchoMode.OFF)[0].hint() == ""


def test_the_guard_protects_while_speaking_and_for_the_tail_after() -> None:
    guard, state = make_guard(tail_ms=100)
    assert guard.protecting(30.0) is False
    state["speaking"] = True
    assert guard.protecting(30.0) is True
    state["speaking"] = False
    assert [guard.protecting(30.0) for _ in range(6)] == [True] * 4 + [
        False
    ] * 2  # 100 ms = 4 frames


def test_headphones_are_never_protected() -> None:
    guard, state = make_guard(OutputKind.HEADPHONES)
    state["speaking"] = True
    assert guard.protecting(30.0) is False


# ---- looking at the device again ----------------------------------------------------------------


def spawning() -> tuple[list[Callable[[], None]], Callable[[Callable[[], None]], None]]:
    queued: list[Callable[[], None]] = []
    return queued, queued.append


def test_the_output_is_looked_at_again_when_nox_starts_speaking_but_not_too_often() -> None:
    queued, spawn = spawning()
    clock = Clock()
    probe = FakeProbe(OutputKind.LOUDSPEAKER)
    state = {"speaking": False}
    guard = EchoGuard(
        params=EchoParams(refresh_s=30.0),
        probe=probe,
        speaking=lambda: state["speaking"],
        spawn=spawn,
        clock=clock,
    )

    state["speaking"] = True
    guard.protecting(30.0)
    assert len(queued) == 1  # never probed yet
    queued.pop()()
    assert probe.asked == [""] and guard.kind is OutputKind.LOUDSPEAKER

    state["speaking"] = False
    guard.protecting(30.0)
    clock.now += 10
    state["speaking"] = True
    guard.protecting(30.0)
    assert queued == []  # 10 s since the last look: too soon

    state["speaking"] = False
    guard.protecting(30.0)
    clock.now += 25
    state["speaking"] = True
    guard.protecting(30.0)
    assert len(queued) == 1  # 35 s: look again


def test_only_the_start_of_speech_triggers_a_look_not_every_frame() -> None:
    queued, spawn = spawning()
    state = {"speaking": True}
    guard = EchoGuard(probe=FakeProbe(), speaking=lambda: state["speaking"], spawn=spawn)
    for _ in range(10):
        guard.protecting(30.0)
    assert len(queued) == 1


def test_a_look_that_is_still_running_is_not_started_twice() -> None:
    queued, spawn = spawning()
    state = {"speaking": False}
    guard = EchoGuard(probe=FakeProbe(), speaking=lambda: state["speaking"], spawn=spawn)
    guard.refresh_now()
    guard.refresh_now()
    assert len(queued) == 1
    queued.pop()()
    guard.refresh_now()
    assert len(queued) == 1  # finished: a new one may start


def test_output_that_is_not_played_here_is_never_looked_at_again() -> None:
    queued, spawn = spawning()
    guard = EchoGuard(probe=FakeProbe(), listens_here=False, speaking=lambda: True, spawn=spawn)
    guard.protecting(30.0)
    assert queued == []


def test_a_changed_device_is_looked_at_with_the_new_name() -> None:
    queued, spawn = spawning()
    probe = FakeProbe(OutputKind.HEADPHONES)
    guard = EchoGuard(probe=probe, device="desk", spawn=spawn)
    guard.set_device("headset")
    queued.pop()()
    assert probe.asked == ["headset"]
    assert guard.kind is OutputKind.HEADPHONES


def test_a_device_that_is_no_longer_played_here_becomes_silent() -> None:
    queued, spawn = spawning()
    guard = EchoGuard(probe=FakeProbe(OutputKind.LOUDSPEAKER), spawn=spawn)
    guard.set_device("", listens_here=False)
    queued.pop()()
    assert guard.kind is OutputKind.SILENT


def test_the_speaking_state_can_be_bound_after_construction() -> None:
    guard = EchoGuard(probe=FakeProbe(), speaking=lambda: False)
    guard.detect()
    guard.bind_speaking(lambda: True)
    assert guard.protecting(30.0) is True


# ---- the rule -----------------------------------------------------------------------------------


def test_nothing_changes_while_nox_is_silent() -> None:
    vad, inner, _ = guarded()
    assert [vad.is_speech(const_frame(-60)) for _ in range(5)] == [True] * 5
    assert inner.seen == 5


def test_echo_level_speech_never_counts_while_nox_speaks() -> None:
    vad, _, state = guarded(start_db=-30.0)
    state["speaking"] = True
    assert not any(vad.is_speech(const_frame(-40)) for _ in range(100))


def test_a_loud_voice_counts_after_the_minimum_duration_and_not_before() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    results = [vad.is_speech(const_frame(-20)) for _ in range(10)]
    assert results == [False] * 7 + [True] * 3  # 7 x 30 ms = 210 ms, the 8th reaches 240 ms


def test_the_level_threshold_is_inclusive() -> None:
    at = const_frame(-30.0)
    assert frame_rms_db(at) == pytest.approx(-30.0, abs=0.01)
    vad, _, state = guarded(start_db=frame_rms_db(at), min_ms=30)
    state["speaking"] = True
    assert vad.is_speech(at) is True
    vad, _, state = guarded(start_db=frame_rms_db(at) + 0.5, min_ms=30)
    state["speaking"] = True
    assert vad.is_speech(at) is False


def test_a_dip_shorter_than_the_hold_time_does_not_interrupt_the_count() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    frames = [const_frame(-20)] * 6 + [const_frame(-45)] + [const_frame(-20)] * 3
    results = [vad.is_speech(f) for f in frames]
    assert results == [False] * 8 + [True] * 2  # the dip adds nothing, takes nothing: 8 loud frames


def test_the_dips_between_syllables_do_not_keep_a_real_voice_out() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    syllable = [const_frame(-20)] * 3 + [const_frame(-45)] * 3  # 180 ms, half of it a dip
    results = [vad.is_speech(f) for _ in range(4) for f in syllable]
    assert results.index(True) <= 13  # counted during the third syllable


def test_a_long_quiet_stretch_makes_the_count_run_down() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    for _ in range(7):  # 210 ms: one frame short
        vad.is_speech(const_frame(-20))
    for _ in range(12):  # the hold runs out after 150 ms, then the count falls for 7 frames
        vad.is_speech(const_frame(-45))
    assert vad.is_speech(const_frame(-20)) is False  # starts from nothing again


def test_echo_whose_peaks_stay_below_the_level_never_adds_up() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    echo_with_peaks = ([const_frame(-42)] * 5 + [const_frame(-33)]) * 100
    assert not any(vad.is_speech(f) for f in echo_with_peaks)


def test_a_zero_minimum_duration_lets_a_loud_frame_straight_through() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=0)
    state["speaking"] = True
    assert vad.is_speech(const_frame(-20)) is True


def test_once_counted_the_voice_stays_counted_through_quiet_stretches() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    for _ in range(8):
        vad.is_speech(const_frame(-20))
    assert vad.is_speech(const_frame(-45)) is True  # quieter, still the same voice


def test_a_pause_in_the_voice_does_not_cost_the_start_rule_again_if_short() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    for _ in range(8):
        vad.is_speech(const_frame(-20))
    for _ in range(5):  # 150 ms of silence
        assert vad.is_speech(const_frame(-80)) is False
    assert vad.is_speech(const_frame(-45)) is True  # the voice resumes, quieter


def test_a_pause_of_exactly_the_hang_time_is_still_the_same_voice() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    for _ in range(8):
        vad.is_speech(const_frame(-20))
    for _ in range(10):  # 300 ms
        vad.is_speech(const_frame(-80))
    assert vad.is_speech(const_frame(-45)) is True


def test_after_a_long_pause_the_start_rule_applies_again() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    for _ in range(8):
        vad.is_speech(const_frame(-20))
    for _ in range(12):  # 360 ms of silence: past the 300 ms the rule is kept open
        vad.is_speech(const_frame(-80))
    assert vad.is_speech(const_frame(-45)) is False  # echo-level again: not the user


def test_what_the_detector_does_not_call_speech_is_never_speech() -> None:
    vad, _, state = guarded(min_ms=0)
    state["speaking"] = True
    assert vad.is_speech(const_frame(-90)) is False  # loud enough? no: the detector says no


def test_half_duplex_hears_nothing_while_nox_speaks_but_everything_afterwards() -> None:
    vad, inner, state = guarded(mode=EchoMode.HALF_DUPLEX, tail_ms=60)
    state["speaking"] = True
    assert not any(vad.is_speech(const_frame(-10)) for _ in range(30))
    state["speaking"] = False
    assert [vad.is_speech(const_frame(-10)) for _ in range(4)] == [
        False,
        False,
        True,
        True,
    ]  # 60 ms
    assert inner.seen == 34  # the detector still saw every frame


def test_the_tail_after_nox_stops_still_holds_back_echo() -> None:
    vad, _, state = guarded(start_db=-30.0, tail_ms=120)
    state["speaking"] = True
    vad.is_speech(const_frame(-40))
    state["speaking"] = False
    assert [vad.is_speech(const_frame(-40)) for _ in range(6)] == [False] * 4 + [True] * 2


@pytest.mark.parametrize("kind", [OutputKind.HEADPHONES, OutputKind.SILENT])
def test_headphones_and_silent_outputs_leave_the_detector_alone(kind: OutputKind) -> None:
    vad, _, state = guarded(kind)
    state["speaking"] = True
    assert vad.is_speech(const_frame(-60)) is True


def test_the_rule_switched_off_leaves_the_detector_alone() -> None:
    vad, _, state = guarded(mode=EchoMode.OFF)
    state["speaking"] = True
    assert vad.is_speech(const_frame(-60)) is True


def test_a_forced_threshold_rule_applies_even_to_headphones() -> None:
    vad, _, state = guarded(OutputKind.HEADPHONES, mode=EchoMode.THRESHOLD)
    state["speaking"] = True
    assert vad.is_speech(const_frame(-45)) is False


def test_reset_clears_the_run_and_resets_the_detector() -> None:
    vad, inner, state = guarded(min_ms=240)
    state["speaking"] = True
    for _ in range(8):
        vad.is_speech(const_frame(-20))
    vad.reset()
    assert inner.resets == 1
    assert vad.is_speech(const_frame(-20)) is False  # the run starts from nothing again


def test_the_frame_length_sets_the_pace_of_the_minimum_duration() -> None:
    vad, _, state = guarded(min_ms=240)
    state["speaking"] = True
    long_frame = const_frame(-20, n=fx.SR * 120 // 1000)  # 120 ms each
    assert [vad.is_speech(long_frame) for _ in range(3)] == [False, True, True]


# ---- with a real segmenter and pipeline ---------------------------------------------------------


def segmenter_with_echo_rule(speaking: Callable[[], bool]) -> Segmenter:
    guard = EchoGuard(
        params=EchoParams(start_db=-30.0, min_ms=240, tail_ms=400),
        probe=FakeProbe(OutputKind.LOUDSPEAKER),
        speaking=speaking,
    )
    guard.detect()
    return Segmenter(
        vad=EchoGuardedVad(EnergyVad(), guard),
        sample_rate=16000,
        end_silence_ms=300,
        min_segment_ms=200,
        pre_roll_ms=60,
    )


def tone_frames(ms: int, amp: float) -> list[np.ndarray]:
    return fx.frames(
        (amp * np.sin(2 * np.pi * 220 * np.arange(fx.SR * ms // 1000) / fx.SR)).astype(np.float32)
    )


def test_the_echo_of_nox_does_not_open_a_segment_but_a_voice_over_it_does() -> None:
    state = {"speaking": True}
    segmenter = segmenter_with_echo_rule(lambda: state["speaking"])
    events = [segmenter.push(f) for f in tone_frames(900, 0.01)]  # about -43 dBFS: room echo
    assert [e for e in events if e is not None] == []
    assert not segmenter.active
    events = [segmenter.push(f) for f in tone_frames(600, 0.2)]  # about -17 dBFS: a person
    assert [e.kind for e in events if e is not None] == ["start"]
    assert segmenter.active


async def test_the_pipeline_does_not_interrupt_itself_on_its_own_echo(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    pipeline_box: dict[str, DefaultVoicePipeline] = {}
    segmenter = segmenter_with_echo_rule(lambda: pipeline_box["p"].speaking)
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    pipeline = DefaultVoicePipeline(
        audio_in=audio_in,
        audio_out=audio_out,
        stt=FakeStt(),
        tts=FakeTts(chunks=200, delay=0.005),
        emit=emit,
        capture_allowed=lambda: True,
        config=PipelineConfig(),
        segmenter=segmenter,
    )
    pipeline_box["p"] = pipeline
    await pipeline.start()
    say = asyncio.create_task(pipeline.say(TtsRequest(utterance_id="u1", text="Eine lange Rede.")))
    await asyncio.sleep(0.02)

    audio_in.push_all(tone_frames(900, 0.01))  # the echo of Nox's own voice
    await settle(60)
    assert audio_out.stops == []
    assert E.VOICE_INPUT_STARTED not in [name for name, _ in events]

    audio_in.push_all(tone_frames(600, 0.2))  # the user talks over Nox
    await settle(60)
    await say
    assert audio_out.stops == ["barge_in"]
    assert E.VOICE_INPUT_STARTED in [name for name, _ in events]
    await pipeline.stop()


# ---- with the real model and speech-like signals ----------------------------------------------


def test_the_real_detector_and_the_rule_keep_quiet_speech_like_echo_out() -> None:
    pytest.importorskip("onnxruntime")
    from nox.voice.vad_silero import SileroModelError, build_vad, find_model

    try:
        find_model("", None)
    except SileroModelError:
        pytest.skip("no Silero model (faster-whisper is not installed)")
    detector = build_vad()
    assert detector.engine == "silero"
    guard, state = make_guard(start_db=-30.0, min_ms=240)
    vad = EchoGuardedVad(detector, guard)
    state["speaking"] = True

    echo_of_nox = fx.scaled_to_db(fx.speech_like(2.0), -42.0)  # speech, but only the room's echo
    assert sum(vad.is_speech(f) for f in fx.frames(echo_of_nox)) == 0

    vad.reset()
    user = fx.scaled_to_db(fx.speech_like(2.0, seed=5), -22.0)
    decisions = [vad.is_speech(f) for f in fx.frames(user)]
    assert sum(decisions) > 0.5 * len(decisions)  # the quiet vowels of the signal come late
    assert not decisions[0]  # not before the minimum duration


def test_silence_inside_the_hold_time_still_runs_the_count_down() -> None:
    vad, _, state = guarded(start_db=-30.0, min_ms=240)
    state["speaking"] = True
    for _ in range(7):  # 210 ms
        vad.is_speech(const_frame(-20))
    for _ in range(2):  # the detector hears no speech: two frames come off the count
        assert vad.is_speech(const_frame(-80)) is False
    results = [vad.is_speech(const_frame(-20)) for _ in range(3)]
    assert results == [False, False, True]  # 150 -> 180, 210, 240


def test_the_output_is_looked_at_once_per_speech_not_once_per_frame() -> None:
    probe = FakeProbe()
    state = {"speaking": True}
    guard = EchoGuard(
        params=EchoParams(refresh_s=0.0),  # no throttle: only the start of speech may trigger
        probe=probe,
        speaking=lambda: state["speaking"],
        spawn=lambda work: work(),
    )
    for _ in range(10):
        guard.protecting(30.0)
    assert probe.asked == [""]
    state["speaking"] = False
    guard.protecting(30.0)
    state["speaking"] = True
    guard.protecting(30.0)
    assert probe.asked == ["", ""]


@pytest.mark.parametrize(
    ("kind", "listens", "expected"),
    [
        (OutputKind.LOUDSPEAKER, True, True),
        (OutputKind.UNKNOWN, True, True),
        (OutputKind.HEADPHONES, True, False),
        (OutputKind.SILENT, True, False),
        (OutputKind.LOUDSPEAKER, False, False),
    ],
)
def test_the_guard_says_whether_nox_plays_through_loudspeakers(
    kind: OutputKind, listens: bool, expected: bool
) -> None:
    guard = EchoGuard(probe=FakeProbe(kind), listens_here=listens)
    guard.detect()
    assert guard.plays_on_loudspeakers is expected
