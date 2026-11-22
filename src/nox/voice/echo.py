"""Echo protection, version 1: do not mistake Nox's own voice for the user's.

The problem: with the microphone open while Nox talks through loudspeakers, the microphone hears
Nox. A speech detector cannot tell that voice from the user's, so Nox "interrupts itself" and
sometimes answers its own words. Headphones make this a non-issue; loudspeakers need a rule.

What this module does (and does not):

- It finds out what kind of output Nox plays on (`OutputKind`): headphones, loudspeakers, nothing
  audible here (stream-only or muted) or unknown. Windows reads the endpoint's *form factor* from
  the audio endpoint registry (the value the sound settings use for the device icon), Linux asks
  PulseAudio/PipeWire (`pactl`), macOS asks `system_profiler`; every platform falls back to a
  words-in-the-device-name guess. The platform code sits behind `OutputProbe` and is told what to
  read, so tests drive it with fakes and never touch a sound device.
- While Nox is speaking over loudspeakers, and for `tail_ms` after it, the guard holds the
  detector's start to a stricter rule (`EchoGuardedVad`): speech must be above `start_db` for
  `min_ms` in a row before it counts. The room's echo of a normally played voice is quieter than a
  person talking at the microphone, so a real interruption passes and the echo does not. In
  `half_duplex` mode nothing counts while Nox speaks (push-to-talk still works: it does not use the
  detector).
- Headphones, unknown-but-not-needed outputs and `mode: off` leave the detector untouched.

What it cannot do: it is a threshold, not echo cancellation. A very loud output or a microphone
next to the speaker defeats it, and the user talking quietly over loud speakers is not heard. Real
echo cancellation (subtracting the known output from the input) is a separate piece of work; until
then the dashboard's health line says "headphones recommended" whenever the output is not
headphones.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import numpy as np

from nox.voice._logging import get_logger
from nox.voice.vad import Vad, frame_rms_db

log = get_logger(__name__)


class OutputKind(StrEnum):
    HEADPHONES = "headphones"
    LOUDSPEAKER = "loudspeaker"
    #: Nothing is played on this machine's speakers (stream-only routing, or muted).
    SILENT = "silent"
    UNKNOWN = "unknown"


class EchoMode(StrEnum):
    #: Headphones and silent outputs: nothing changes. Loudspeakers and unknown outputs: threshold.
    AUTO = "auto"
    OFF = "off"
    THRESHOLD = "threshold"
    HALF_DUPLEX = "half_duplex"


@dataclass(frozen=True, slots=True)
class EchoParams:
    mode: EchoMode = EchoMode.AUTO
    #: While Nox speaks, the detector's speech must be at least this loud (dBFS, RMS per frame) ...
    start_db: float = -30.0
    #: ... for this long in a row before it counts as the user speaking.
    min_ms: int = 240
    #: The room still rings for a moment after Nox stops; the rule stays on for this long.
    tail_ms: int = 400
    #: How often the output device is looked at again (seconds), at the start of Nox's speech.
    refresh_s: float = 30.0


# ---- classifying a device ------------------------------------------------------------------------

_HEADPHONE_WORDS = (
    "headphone",
    "headset",
    "kopfh",
    "earphone",
    "earbud",
    "airpods",
    "in-ear",
    "hands-free",
    "handsfree",
    "casque",
    "auricular",
    "cuffie",
)
_SPEAKER_WORDS = ("speaker", "lautsprecher", "haut-parleur", "altavo", "altoparlant", "soundbar")
_VIRTUAL_WORDS = ("virtual", "cable input", "cable in ", "voicemeeter", "loopback", "blackhole")


def classify_name(name: str) -> OutputKind:
    """A guess from the words in a device name; headphone words win ("Headset Speaker")."""
    lowered = name.casefold()
    if any(word in lowered for word in _VIRTUAL_WORDS):
        return OutputKind.SILENT
    if any(word in lowered for word in _HEADPHONE_WORDS):
        return OutputKind.HEADPHONES
    if any(word in lowered for word in _SPEAKER_WORDS):
        return OutputKind.LOUDSPEAKER
    return OutputKind.UNKNOWN


#: Windows `PKEY_AudioEndpoint_FormFactor` values (mmdeviceapi.h `EndpointFormFactor`).
_FORM_FACTORS: dict[int, OutputKind] = {
    1: OutputKind.LOUDSPEAKER,  # Speakers
    3: OutputKind.HEADPHONES,  # Headphones
    5: OutputKind.HEADPHONES,  # Headset
    6: OutputKind.HEADPHONES,  # Handset
    9: OutputKind.UNKNOWN,  # HDMI / DisplayPort: a TV, a monitor or headphones on the monitor
}


def kind_from_form_factor(form_factor: int, name: str = "") -> OutputKind:
    """The kind for a Windows form factor; a headphone-like name overrides a "Speakers" factor.

    Some jack-sensing sound cards keep the form factor "Speakers" when headphones are plugged in
    and only rename the endpoint.
    """
    kind = _FORM_FACTORS.get(form_factor, OutputKind.UNKNOWN)
    by_name = classify_name(name)
    if by_name in (OutputKind.HEADPHONES, OutputKind.SILENT):
        return by_name
    return kind


# ---- probes --------------------------------------------------------------------------------------


class OutputProbe(Protocol):
    """Says what kind of output a device setting names. Blocking; "not found" is `UNKNOWN`."""

    def kind(self, device: str) -> OutputKind: ...


class NameProbe:
    """The fallback on every platform: the setting (or the resolved name) is all there is."""

    def __init__(self, resolve: Callable[[str], str] | None = None) -> None:
        self._resolve = resolve

    def kind(self, device: str) -> OutputKind:
        name = self._resolve(device) if self._resolve is not None else device
        return classify_name(name)


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One Windows render endpoint as the sound settings know it."""

    name: str
    adapter: str
    form_factor: int
    active: bool

    @property
    def friendly_name(self) -> str:
        return f"{self.name} ({self.adapter})" if self.adapter else self.name


class WindowsEndpointProbe:
    """Matches the device PortAudio uses to an audio endpoint and reads its form factor.

    `endpoints` lists the active render endpoints; `resolve` turns the device setting into the
    full device name PortAudio uses (empty setting = the system default). MME truncates names to
    31 characters, so a name is matched as a prefix too. Two different answers for one name mean
    the name cannot be told apart: `UNKNOWN`.
    """

    def __init__(
        self,
        endpoints: Callable[[], Sequence[Endpoint]],
        resolve: Callable[[str], str],
    ) -> None:
        self._endpoints = endpoints
        self._resolve = resolve

    def kind(self, device: str) -> OutputKind:
        name = self._resolve(device)
        if not name:
            return OutputKind.UNKNOWN
        wanted = name.casefold()
        found: set[OutputKind] = set()
        for endpoint in self._endpoints():
            if not endpoint.active:
                continue
            friendly = endpoint.friendly_name.casefold()
            if friendly == wanted or (len(wanted) >= 31 and friendly.startswith(wanted)):
                found.add(kind_from_form_factor(endpoint.form_factor, endpoint.name))
        if len(found) == 1:
            return found.pop()
        return classify_name(name) if not found else OutputKind.UNKNOWN


_MM_RENDER = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
_PKEY_FORM_FACTOR = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},0"
_PKEY_ENDPOINT_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_PKEY_ADAPTER_NAME = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
_DEVICE_STATE_ACTIVE = 1


def read_windows_endpoints() -> list[Endpoint]:
    """The render endpoints from the registry, read-only. Empty on any other platform or error."""
    if sys.platform != "win32":
        return []
    import winreg

    endpoints: list[Endpoint] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MM_RENDER)
    except OSError:
        return []
    with root:
        index = 0
        while True:
            try:
                guid = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            endpoint = _read_endpoint(winreg, root, guid)
            if endpoint is not None:
                endpoints.append(endpoint)
    return endpoints


def _read_endpoint(winreg: Any, root: Any, guid: str) -> Endpoint | None:
    try:
        with winreg.OpenKey(root, guid) as key:
            state, _ = winreg.QueryValueEx(key, "DeviceState")
        with winreg.OpenKey(root, guid + r"\Properties") as props:
            form_factor, _ = winreg.QueryValueEx(props, _PKEY_FORM_FACTOR)
            name, _ = winreg.QueryValueEx(props, _PKEY_ENDPOINT_NAME)
            try:
                adapter, _ = winreg.QueryValueEx(props, _PKEY_ADAPTER_NAME)
            except OSError:
                adapter = ""
    except OSError:
        return None
    if not isinstance(form_factor, int) or not isinstance(name, str):
        return None
    return Endpoint(
        name=name,
        adapter=adapter if isinstance(adapter, str) else "",
        form_factor=form_factor,
        active=state == _DEVICE_STATE_ACTIVE,
    )


def resolve_output_name(setting: str) -> str:
    """The full device name PortAudio uses for an output setting; '' when it cannot be resolved.

    An empty setting is the system default: on Windows the default of the WASAPI host API, whose
    names are the endpoints' full friendly names (the MME default is the "Sound Mapper", which
    names no endpoint).
    """
    try:
        import sounddevice as sd

        from nox.voice.audio import resolve_device

        index = resolve_device(setting, kind="output")
        if index is None:
            index = _default_output_index(sd)
        if index is None:
            return ""
        return str(sd.query_devices(index)["name"])
    except Exception as exc:  # noqa: BLE001 - an unreadable device is "unknown", not a failure
        log.debug("echo.device_unresolved", error=f"{type(exc).__name__}: {exc}")
        return ""


def _default_output_index(sd: Any) -> int | None:
    for api in sd.query_hostapis():
        if api["name"] == "Windows WASAPI" and api["default_output_device"] >= 0:
            return int(api["default_output_device"])
    default = sd.default.device[1]
    return int(default) if default is not None and default >= 0 else None


Runner = Callable[[list[str]], str]


def run_command(argv: list[str]) -> str:
    """Run a short read-only command and return stdout; '' on any failure."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, never a shell
            argv, capture_output=True, text=True, timeout=3.0, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


class PactlProbe:
    """Linux: PulseAudio and PipeWire's pulse layer. Reads the sink list as JSON, read-only."""

    def __init__(self, run: Runner = run_command) -> None:
        self._run = run

    def kind(self, device: str) -> OutputKind:
        import json

        try:
            sinks = json.loads(self._run(["pactl", "--format=json", "list", "sinks"]) or "[]")
            default = self._run(["pactl", "get-default-sink"]).strip()
        except ValueError:
            return classify_name(device)
        if not isinstance(sinks, list) or not sinks:
            return classify_name(device)
        sink = _pick_sink(sinks, device, default)
        return _sink_kind(sink) if sink is not None else classify_name(device)


def _pick_sink(sinks: list[Any], device: str, default: str) -> dict[str, Any] | None:
    needle = device.strip().casefold()
    named = [s for s in sinks if isinstance(s, dict)]
    if needle:
        for sink in named:
            if needle in f"{sink.get('name', '')} {sink.get('description', '')}".casefold():
                return sink
    for sink in named:
        if default and sink.get("name") == default:
            return sink
    return named[0] if len(named) == 1 else None


def _sink_kind(sink: dict[str, Any]) -> OutputKind:
    description = str(sink.get("description", ""))
    raw_props = sink.get("properties")
    props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
    factor = str(props.get("device.form_factor", "")).casefold()
    if factor in ("headphone", "headset", "handset", "hands-free", "hands_free"):
        return OutputKind.HEADPHONES
    if factor in ("speaker", "tv", "computer", "hifi", "portable", "car"):
        return OutputKind.LOUDSPEAKER
    active = str(sink.get("active_port", ""))
    for port in sink.get("ports") or []:
        if isinstance(port, dict) and port.get("name") == active:
            port_type = str(port.get("type", "")).casefold()
            if port_type in ("headphones", "headset", "handsfree", "earpiece"):
                return OutputKind.HEADPHONES
            if port_type in ("speaker", "line", "hdmi", "spdif"):
                return OutputKind.LOUDSPEAKER
    return classify_name(description or str(sink.get("name", "")))


class MacProbe:
    """macOS: the default output device from `system_profiler`, read-only."""

    def __init__(self, run: Runner = run_command) -> None:
        self._run = run

    def kind(self, device: str) -> OutputKind:
        import json

        try:
            data = json.loads(self._run(["system_profiler", "SPAudioDataType", "-json"]) or "{}")
        except ValueError:
            return classify_name(device)
        wanted = device.strip().casefold()
        for group in data.get("SPAudioDataType", []) if isinstance(data, dict) else []:
            for item in group.get("_items", []) if isinstance(group, dict) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("_name", ""))
                is_default = item.get("coreaudio_default_audio_output_device") == "spaudio_yes"
                if (wanted and wanted in name.casefold()) or (not wanted and is_default):
                    return _mac_kind(name, str(item.get("coreaudio_device_transport", "")))
        return classify_name(device)


def _mac_kind(name: str, transport: str) -> OutputKind:
    by_name = classify_name(name)
    if by_name is not OutputKind.UNKNOWN:
        return by_name
    if "bluetooth" in transport:
        return OutputKind.HEADPHONES
    if "builtin" in transport:
        return OutputKind.LOUDSPEAKER
    return OutputKind.UNKNOWN


def default_probe() -> OutputProbe:
    """The probe for this platform."""
    if sys.platform == "win32":
        return WindowsEndpointProbe(read_windows_endpoints, resolve_output_name)
    if sys.platform == "darwin":
        return MacProbe()
    if sys.platform.startswith("linux"):
        return PactlProbe()
    return NameProbe()


# ---- the guard -----------------------------------------------------------------------------------


class EchoGuard:
    """Holds the output kind and decides whether the stricter start rule is on right now.

    `speaking` says whether Nox is speaking (the pipeline's own state). `listens_here` is False when
    the output is routed away from this machine's speakers (stream-only, muted): then there is
    nothing to echo and the kind is `SILENT` whatever the device is.
    """

    def __init__(
        self,
        *,
        params: EchoParams | None = None,
        probe: OutputProbe | None = None,
        device: str = "",
        listens_here: bool = True,
        speaking: Callable[[], bool] = lambda: False,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.params = params or EchoParams()
        self._probe = probe or default_probe()
        self._device = device
        self._listens_here = listens_here
        self._speaking = speaking
        self._spawn = spawn or _spawn_thread
        self._clock = clock or time.monotonic
        self._kind = OutputKind.UNKNOWN if listens_here else OutputKind.SILENT
        self._probed_at: float | None = None
        self._refreshing = False
        self._was_speaking = False
        self._tail_ms_left = 0.0

    def bind_speaking(self, speaking: Callable[[], bool]) -> None:
        """Late binding: the guard is built before the pipeline whose state it reads."""
        self._speaking = speaking

    def set_device(self, device: str, *, listens_here: bool | None = None) -> None:
        """The output device (or whether it is played here at all) changed: look again soon."""
        self._device = device
        if listens_here is not None:
            self._listens_here = listens_here
        self._probed_at = None
        self.refresh_now()

    def refresh_now(self) -> None:
        """Probe in the background; the answer applies from the next frame it arrives before."""
        if self._refreshing:
            return
        self._refreshing = True

        def work() -> None:
            try:
                self.detect()
            finally:
                self._refreshing = False

        self._spawn(work)

    # -- what kind of output ------------------------------------------------------------------

    @property
    def kind(self) -> OutputKind:
        return self._kind

    def detect(self) -> OutputKind:
        """Look at the output device now (blocking: registry, PortAudio or a command)."""
        if not self._listens_here:
            self._kind = OutputKind.SILENT
        else:
            try:
                self._kind = self._probe.kind(self._device)
            except Exception as exc:  # noqa: BLE001 - detection must never stop capture
                log.warning("echo.detect_failed", error=f"{type(exc).__name__}: {exc}")
                self._kind = OutputKind.UNKNOWN
        self._probed_at = self._clock()
        log.info("echo.output_kind", kind=str(self._kind), mode=str(self.params.mode))
        return self._kind

    @property
    def plays_on_loudspeakers(self) -> bool:
        """True while Nox's voice may reach the microphone through the room: the output is
        loudspeakers or not known to be anything else. False for headphones and for output that
        is not played on this machine. The pipeline may use this (for instance to decide how
        readily its own voice counts as an interruption); the answer can change when the
        output device does."""
        return self._listens_here and self._kind in (OutputKind.LOUDSPEAKER, OutputKind.UNKNOWN)

    @property
    def policy(self) -> EchoMode:
        """The rule that applies: `AUTO` resolved by the output kind. Never `AUTO` itself."""
        mode = self.params.mode
        if mode is not EchoMode.AUTO:
            return mode
        if self._kind in (OutputKind.HEADPHONES, OutputKind.SILENT):
            return EchoMode.OFF
        return EchoMode.THRESHOLD

    def hint(self) -> str:
        """A health note for the dashboard, '' when there is nothing to say."""
        if self.params.mode is EchoMode.OFF or self._kind in (
            OutputKind.HEADPHONES,
            OutputKind.SILENT,
        ):
            return ""
        what = "loudspeakers" if self._kind is OutputKind.LOUDSPEAKER else "an unidentified output"
        return (
            f"headphones recommended: Nox plays through {what}, so the microphone can pick up "
            "Nox's own voice"
        )

    # -- per frame ----------------------------------------------------------------------------

    def protecting(self, frame_ms: float) -> bool:
        """True while the stricter rule applies: Nox speaks, or spoke less than `tail_ms` ago."""
        speaking = bool(self._speaking())
        if speaking and not self._was_speaking:
            self._refresh_soon()
        self._was_speaking = speaking
        if speaking:
            self._tail_ms_left = float(self.params.tail_ms)
            active = True
        elif self._tail_ms_left > 0:
            active = True
            self._tail_ms_left -= frame_ms
        else:
            active = False
        return active and self.policy is not EchoMode.OFF

    def _refresh_soon(self) -> None:
        if self._refreshing or not self._listens_here:
            return
        if self._probed_at is not None and (
            self._clock() - self._probed_at < self.params.refresh_s
        ):
            return
        self.refresh_now()


def _spawn_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="echo-detect", daemon=True).start()


class EchoGuardedVad:
    """Wraps a detector with the echo rule. The inner detector always sees every frame.

    While the guard protects, speech counts only once `min_ms` of it - at `start_db` or louder - has
    been heard. A voice rises and falls several times a second, so a quiet frame within `HOLD_MS`
    of a loud one leaves the count alone; the count grows by one frame for every loud speech frame
    and shrinks by one for every other, so the room's quiet echo of Nox never gets anywhere. Once
    the voice counts, it keeps counting through pauses of up to `HANG_MS`; a longer pause means the
    next sound has to earn its place again.
    """

    #: A quiet frame this soon after a loud one is a dip in a voice, not the end of it.
    HOLD_MS = 150.0
    #: How long a counted voice may be silent before the start rule applies to it again.
    HANG_MS = 300.0

    def __init__(self, inner: Vad, guard: EchoGuard, *, sample_rate: int = 16000) -> None:
        self.inner = inner
        self.guard = guard
        self._sample_rate = sample_rate
        self._run_ms = 0.0
        self._gap_ms = 0.0
        self._open = False
        self._levels: list[float] = []

    def reset(self) -> None:
        self.inner.reset()
        self._close()

    def _close(self) -> None:
        self._run_ms = 0.0
        self._gap_ms = 0.0
        self._open = False
        self._levels = []

    def _held_level(self, level_db: float, frame_ms: float) -> float:
        self._levels.append(level_db)
        del self._levels[: -max(1, round(self.HOLD_MS / frame_ms))]
        return max(self._levels)

    def is_speech(self, frame: np.ndarray) -> bool:
        decision = self.inner.is_speech(frame)
        frame_ms = frame.size * 1000.0 / self._sample_rate
        if not self.guard.protecting(frame_ms):
            self._close()
            return decision
        if self.guard.policy is EchoMode.HALF_DUPLEX:
            return False
        if self._open:
            if decision:
                self._gap_ms = 0.0
                return True
            self._gap_ms += frame_ms
            if self._gap_ms > self.HANG_MS:
                self._close()
            return False
        own = frame_rms_db(frame)
        held = self._held_level(own, frame_ms)
        start_db = self.guard.params.start_db
        if decision and own >= start_db:
            self._run_ms += frame_ms
        elif not (decision and held >= start_db):
            self._run_ms = max(0.0, self._run_ms - frame_ms)  # else: a dip inside the hold time
        if self._run_ms >= self.guard.params.min_ms:
            self._open = True
            self._gap_ms = 0.0
        return self._open and decision
