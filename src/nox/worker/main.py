"""Voice worker process: hosts STT + TTS + audio devices and talks to the core hub over IPC.

Implements the worker side of the Process Model / IPC Model (worker.register, worker.heartbeat every
2 s, inbound `tts.speak`, `tts.stop`, `stt.transcribe`, `voice.ptt`, `voice.mute`; subscribes to
`security.*` and `privacy.*` to stop or gate capture). `--service stt|tts|voice` selects which
engines are loaded; v0.1 runs everything in one `voice` worker. The worker token comes from the
environment (`NOX_WORKER_TOKEN`), never from the command line, and is never logged.
`--selftest` validates devices, models, playback and a 3 s microphone transcription without a hub.
`--plugin <id>` runs this process as a plugin worker instead (ST-11-01, implemented in
`nox.worker.plugin`); it shares only token and hub-url handling with the voice path.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import time
import wave
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import typer
from pydantic import BaseModel, ValidationError

from nox.core.config import VoiceConfig
from nox.core.events import E
from nox.ipc.errors import IpcError
from nox.ipc.protocol import Envelope
from nox.voice._logging import get_logger
from nox.voice.base import Transcript, TtsRequest, VoicePipeline
from nox.voice.pipeline import DefaultVoicePipeline, Emit, PipelineConfig

log = get_logger(__name__)

DEFAULT_HUB_URL = "ws://127.0.0.1:47800/ws"
CONNECT_DEADLINE_S = 60.0
HEARTBEAT_S = 2.0
SERVICES: dict[str, tuple[str, ...]] = {
    "stt": ("stt", "voice"),
    "tts": ("tts", "voice"),
    "voice": ("stt", "tts", "voice"),
}


class WorkerClient(Protocol):
    """The subset of nox.ipc.client.IpcClient the worker relies on (structural; fakes in tests)."""

    async def connect(self) -> Any: ...
    async def close(self) -> None: ...
    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]: ...
    async def send_event(
        self, name: str, payload: Mapping[str, Any] | None = None, *, corr: str | None = None
    ) -> None: ...
    async def subscribe(self, patterns: Iterable[str], handler: Any = None) -> list[str]: ...
    def on(self, name_glob: str, handler: Callable[[Envelope], Awaitable[None] | None]) -> Any: ...
    def handle(self, name: str, handler: Any) -> None: ...


PipelineFactory = Callable[[Emit, Callable[[], bool], VoiceConfig], VoicePipeline]
ComponentLoader = Callable[[VoiceConfig], Awaitable[tuple[Any, Any]]]


class WorkerStatus(BaseModel):
    service: str
    status: str = "starting"  # starting | running | safe_mode | stopping
    microphone_allowed: bool = True
    killed: bool = False


def decode_audio_ref(payload: Mapping[str, Any]) -> tuple[np.ndarray, int]:
    """`stt.transcribe` payload -> (float32 mono, sample_rate): a WAV path or base64 PCM16."""
    ref = str(payload.get("audio_ref", "") or "")
    if ref:
        path = Path(ref)
        if path.suffix.lower() != ".wav" or not path.is_file():
            raise FileNotFoundError(f"audio_ref must point to an existing .wav file: {ref}")
        with wave.open(str(path), "rb") as wf:
            rate, channels, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if width != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return samples / 32768.0, rate
    b64 = payload.get("audio_b64")
    if not b64:
        raise ValueError("payload needs audio_ref or audio_b64")
    rate = int(payload.get("sample_rate", 16000))
    pcm = np.frombuffer(base64.b64decode(b64), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0, rate


class VoiceWorker:
    def __init__(
        self,
        *,
        client: WorkerClient,
        service: str,
        pipeline_factory: PipelineFactory,
        stt: Any = None,
        tts: Any = None,
        heartbeat_s: float = HEARTBEAT_S,
        load_fn: Callable[[], float] | None = None,
        default_config: VoiceConfig | None = None,
        component_loader: ComponentLoader | None = None,
        connect_deadline_s: float = CONNECT_DEADLINE_S,
        connect_backoff_s: float = 0.5,
    ) -> None:
        if service not in SERVICES:
            raise ValueError(f"unknown service {service!r}")
        self.client = client
        self.connect_deadline_s = connect_deadline_s
        self.connect_backoff_s = connect_backoff_s
        self._ran_once = False
        self._reregister_task: asyncio.Task[None] | None = None
        self.service = service
        self.capabilities = SERVICES[service]
        self.stt = stt
        self.tts = tts
        self.heartbeat_s = heartbeat_s
        self._load_fn = load_fn or (lambda: 0.0)
        self.status = WorkerStatus(service=service)
        self._stop = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._say_tasks: set[asyncio.Task[None]] = set()
        self._pipeline_factory = pipeline_factory
        self._component_loader = component_loader
        self.default_config = default_config or VoiceConfig()
        self.voice_config: VoiceConfig = self.default_config
        # Built only inside run(), after `worker.register` has returned the core's merged config
        # (B-8: register first, apply config, only then build/load engines) — never before.
        self.pipeline: VoicePipeline | None = None
        self.register_response: dict[str, Any] = {}

    # ---- gate + emit -----------------------------------------------------------------------------

    def capture_allowed(self) -> bool:
        return self.status.microphone_allowed and not self.status.killed

    async def emit(self, name: str, payload: dict[str, Any]) -> None:
        if name == E.VOICE_KILL_PHRASE:
            # Local kill phrase: stop everything here first, then tell the core (never via LLM).
            self.status.killed = True
            self.status.status = "safe_mode"
        try:
            await self.client.send_event(name, payload)
        except Exception as exc:  # noqa: BLE001 - IPC hiccups must not break the audio path
            log.warning("worker.emit_failed", name=name, error=str(exc))

    # ---- inbound requests ------------------------------------------------------------------------

    def _register_handlers(self) -> None:
        if "tts" in self.capabilities:
            self.client.handle("tts.speak", self._on_tts_speak)
            self.client.handle("tts.stop", self._on_tts_stop)
        if "stt" in self.capabilities:
            self.client.handle("stt.transcribe", self._on_stt_transcribe)
        self.client.handle("voice.ptt", self._on_voice_ptt)
        self.client.handle("voice.mute", self._on_voice_mute)
        self.client.on(E.SECURITY_KILL_SWITCH, self._on_kill_switch)
        self.client.on(E.SECURITY_PANIC, self._on_panic)
        self.client.on(E.PRIVACY_CAPTURE_CHANGED, self._on_capture_changed)
        self.client.on(E.SYSTEM_STOPPING, self._on_system_stopping)

    def _pipeline_ready(self) -> VoicePipeline:
        """The pipeline is only built in `run()`, after `worker.register`/config are settled."""
        if self.pipeline is None:
            raise RuntimeError("voice pipeline not ready (worker has not finished registering)")
        return self.pipeline

    async def _on_tts_speak(self, payload: dict[str, Any], _req: Any = None) -> dict[str, Any]:
        try:
            request = TtsRequest.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"invalid TtsRequest: {exc.errors()[0]['msg']}") from exc
        if self.status.killed:
            return {"ok": False, "utterance_id": request.utterance_id, "reason": "safe_mode"}
        pipeline = self._pipeline_ready()
        task = asyncio.create_task(pipeline.say(request), name=f"say-{request.utterance_id}")
        self._say_tasks.add(task)
        task.add_done_callback(self._say_tasks.discard)
        return {"ok": True, "utterance_id": request.utterance_id, "accepted": True}

    async def _on_tts_stop(self, payload: dict[str, Any], _req: Any = None) -> dict[str, Any]:
        await self._pipeline_ready().interrupt(reason=str(payload.get("reason", "stop")))
        return {"ok": True}

    async def _on_stt_transcribe(self, payload: dict[str, Any], _req: Any = None) -> dict[str, Any]:
        if self.stt is None:
            raise RuntimeError("stt engine not loaded")
        audio, rate = decode_audio_ref(payload)
        language = str(payload.get("language", "auto") or "auto")
        transcript: Transcript = await self.stt.transcribe(audio, rate, language=language)
        return transcript.model_dump(mode="json")

    async def _on_voice_ptt(self, payload: dict[str, Any], _req: Any = None) -> dict[str, Any]:
        await self._pipeline_ready().push_to_talk(bool(payload.get("pressed", False)))
        return {"ok": True}

    async def _on_voice_mute(self, payload: dict[str, Any], _req: Any = None) -> dict[str, Any]:
        await self._pipeline_ready().set_muted(bool(payload.get("muted", True)))
        return {"ok": True}

    # ---- inbound events --------------------------------------------------------------------------

    async def _on_kill_switch(self, _env: Envelope) -> None:
        self.status.killed = True
        self.status.status = "safe_mode"
        if self.pipeline is not None:
            await self.pipeline.interrupt(reason="kill_switch")
            await self.pipeline.stop()
        log.warning("worker.kill_switch")

    async def _on_panic(self, _env: Envelope) -> None:
        self.status.microphone_allowed = False
        if self.pipeline is not None:
            await self.pipeline.interrupt(reason="panic")
        self._refresh_gate()

    async def _on_capture_changed(self, env: Envelope) -> None:
        self.status.microphone_allowed = bool(env.payload.get("microphone", False))
        self._refresh_gate()

    async def _on_system_stopping(self, _env: Envelope) -> None:
        self._stop.set()

    def _refresh_gate(self) -> None:
        refresh = getattr(self.pipeline, "refresh_gate", None)
        if refresh is not None:
            refresh()

    # ---- lifecycle -------------------------------------------------------------------------------

    async def register(self) -> dict[str, Any]:
        payload = {
            "service": self.service,
            "capabilities": list(self.capabilities),
            "pid": os.getpid(),
        }
        self.register_response = await self.client.request("worker.register", payload)
        return self.register_response

    def _resolve_config(self, raw: Any) -> VoiceConfig:
        """Validate the `config` the core returned from `worker.register` into a `VoiceConfig`.

        Falls back to the worker's local defaults (with a warning) if the core sent nothing or
        something that fails validation - a malformed config must never crash the worker.
        """
        if not raw:
            return self.default_config
        try:
            return VoiceConfig.model_validate(raw)
        except ValidationError as exc:
            log.warning("worker.config_invalid", error=str(exc))
            return self.default_config

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.client.request(
                    "worker.heartbeat",
                    {"load": float(self._load_fn()), "status": self.status.status},
                    timeout=self.heartbeat_s,
                )
            except Exception as exc:  # noqa: BLE001 - the core marks us unavailable after 3 misses
                log.warning("worker.heartbeat_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_s)
            except TimeoutError:
                continue

    async def run(self) -> None:
        # B-8: connect -> worker.register -> apply the core's config -> only then build/load the
        # engines -> worker.ready. Health reports us `limited` ("worker starting") in between.
        self._register_handlers()
        await self._connect_with_retry()
        await self.client.subscribe(["security.*", "privacy.*", "system.stopping"])
        await self.register()
        self._ran_once = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat")
        self.voice_config = self._resolve_config(self.register_response.get("config"))
        if self._component_loader is not None:
            self.stt, self.tts = await self._component_loader(self.voice_config)
        self.pipeline = self._pipeline_factory(self.emit, self.capture_allowed, self.voice_config)
        self.status.status = "running"
        await self.pipeline.start()
        await self.client.request("worker.ready", {"service": self.service})
        log.info("worker.running", service=self.service, capabilities=list(self.capabilities))
        try:
            await self._stop.wait()
        finally:
            await self.shutdown()

    def on_connection_change(self, connected: bool) -> None:
        """IpcClient reconnect hook (sync, called from the client's loop). After the core dropped
        us (restart, stalled hub) the new session knows nothing about this worker: register again
        and, if the pipeline already runs, report ready again."""
        if not connected or not self._ran_once:
            return
        self._reregister_task = asyncio.get_running_loop().create_task(
            self._reregister(), name="worker-reregister"
        )

    async def _reregister(self) -> None:
        try:
            await self.client.subscribe(["security.*", "privacy.*", "system.stopping"])
            await self.register()
            if self.status.status == "running":
                await self.client.request("worker.ready", {"service": self.service})
            log.info("worker.reregistered", service=self.service)
        except Exception as exc:  # noqa: BLE001 - the next reconnect tries again
            log.warning("worker.reregister_failed", error=str(exc))

    async def _connect_with_retry(self) -> None:
        """The core spawns us while it is still booting (extensions import OpenCV and friends
        synchronously), so a hub that does not answer yet is retried with backoff until
        `connect_deadline_s`; non-retryable errors (bad token, protocol) surface at once."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_deadline_s
        delay = self.connect_backoff_s
        while True:
            try:
                await self.client.connect()
                return
            except IpcError as exc:
                if not exc.retryable or loop.time() + delay > deadline:
                    raise
                log.warning("worker.connect_retry", error=str(exc), retry_in_s=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        self.status.status = "stopping"
        self._stop.set()
        if self.pipeline is not None:
            await self.pipeline.stop()
        for task in list(self._say_tasks):
            task.cancel()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        await self.client.close()


# ---- process wiring ----------------------------------------------------------------------------


def raise_priority() -> None:
    """Above-normal priority for the audio path (Process Model). Never touches other processes."""
    if sys.platform != "win32":
        return
    try:
        import psutil

        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
    except Exception as exc:  # noqa: BLE001 - priority is best effort
        log.warning("worker.priority_failed", error=str(exc))


def process_load() -> float:
    try:
        import psutil

        return float(psutil.Process().cpu_percent(interval=None)) / 100.0
    except Exception:  # noqa: BLE001
        return 0.0


def pipeline_config_from(voice: VoiceConfig) -> PipelineConfig:
    return PipelineConfig(
        wake_word=voice.stt.wake_word,
        language=voice.stt.language,
        barge_in=voice.barge_in,
    )


def build_components(voice: VoiceConfig, *, service: str) -> dict[str, Any]:
    """Instantiate engines and devices for a service (models are loaded later, asynchronously)."""
    from nox.voice.audio import SoundDeviceInput, SoundDeviceOutput
    from nox.voice.stt.faster_whisper_engine import FasterWhisperStt
    from nox.voice.tts.piper_engine import PiperTts

    caps = SERVICES[service]
    components: dict[str, Any] = {
        "audio_in": SoundDeviceInput(),
        "audio_out": SoundDeviceOutput(
            private_device=voice.channels.private_device,
            stream_device=voice.channels.stream_device,
            volume=voice.tts.volume,
        ),
        "stt": None,
        "tts": None,
    }
    if "stt" in caps:
        components["stt"] = FasterWhisperStt(model_size=voice.stt.model, device=voice.stt.device)
    if "tts" in caps:
        if voice.tts.engine == "kokoro":
            from nox.voice.tts.kokoro_engine import KokoroTts

            components["tts"] = KokoroTts(rate=voice.tts.rate)
        else:
            voices = {"de": voice.tts.voice} if voice.tts.voice else None
            components["tts"] = PiperTts(voices=voices, rate=voice.tts.rate)
    return components


class _NoEngine:
    """Placeholder for a capability this worker does not host; fails loudly if used."""

    id = "none"
    sample_rate = 16000

    async def health(self) -> tuple[Any, str]:
        from nox.core.events import HealthStatus

        return HealthStatus.UNAVAILABLE, "not hosted by this worker"

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        return None

    async def transcribe(
        self, audio: Any, sample_rate: int, *, language: str = "auto"
    ) -> Transcript:
        raise RuntimeError("stt is not hosted by this worker")

    def synthesize(self, request: TtsRequest) -> Any:
        raise RuntimeError("tts is not hosted by this worker")


async def run_worker(service: str, hub_url: str, token: str, voice_cfg: VoiceConfig) -> int:
    """`connect -> worker.register -> apply config -> build/load engines -> worker.ready` (B-8).

    Engines are built from whatever `VoiceConfig` the core hands back on `worker.register`
    (falling back to `voice_cfg`, the local default, if that config is missing/invalid) - never
    from `voice_cfg` directly - so devices/models always reflect the core's merged config.
    """
    from nox.ipc.client import IpcClient

    built: dict[str, Any] = {}

    async def load_components(cfg: VoiceConfig) -> tuple[Any, Any]:
        components = build_components(cfg, service=service)
        stt = components["stt"] or _NoEngine()
        tts = components["tts"] or _NoEngine()
        await stt.load()
        await tts.load()
        built["audio_in"] = components["audio_in"]
        built["audio_out"] = components["audio_out"]
        return stt, tts

    def factory(emit: Emit, capture_allowed: Callable[[], bool], cfg: VoiceConfig) -> VoicePipeline:
        return DefaultVoicePipeline(
            audio_in=built["audio_in"],
            audio_out=built["audio_out"],
            stt=worker.stt,
            tts=worker.tts,
            emit=emit,
            capture_allowed=capture_allowed,
            config=pipeline_config_from(cfg),
        )

    client = IpcClient(
        hub_url,
        token,
        "worker",
        f"worker:{service}",
        client_version="0.1.0",
        reconnect=True,
        on_connection_change=lambda connected: worker.on_connection_change(connected),
    )
    worker = VoiceWorker(
        client=client,
        service=service,
        pipeline_factory=factory,
        load_fn=process_load,
        default_config=voice_cfg,
        component_loader=load_components,
    )
    try:
        await worker.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await worker.shutdown()
    return 0


async def run_selftest(voice_cfg: VoiceConfig, *, seconds: float = 3.0) -> int:
    """Manual validation: devices, model load, TTS to the default device, 3 s mic transcription."""
    from nox.voice.audio import list_devices

    echo = typer.echo
    echo("== devices ==")
    for dev in list_devices():
        if dev["hostapi"] != "Windows WASAPI":
            continue
        flags = ("IN " if dev["inputs"] != "0" else "   ") + (
            "OUT" if dev["outputs"] != "0" else "   "
        )
        default = (
            " (default)"
            if dev["default_input"] == "True" or dev["default_output"] == "True"
            else ""
        )
        echo(f"  [{dev['index']:>2}] {flags} {dev['name']}{default}")
    components = build_components(voice_cfg, service="voice")
    stt, tts = components["stt"], components["tts"]
    audio_in, audio_out = components["audio_in"], components["audio_out"]
    echo("== models ==")
    t0 = time.perf_counter()
    await stt.load()
    echo(f"  stt {stt.id} {stt.model_size}: {stt.load_time_ms:.0f} ms")
    await tts.load()
    echo(
        f"  tts {tts.id} {tts.loaded_voices}: {tts.load_time_ms:.0f} ms "
        f"(total {time.perf_counter() - t0:.1f} s)"
    )

    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))
        echo(f"  event {name} {payload if name != E.VOICE_TRANSCRIPT_READY else payload['text']!r}")

    pipeline = DefaultVoicePipeline(
        audio_in=audio_in,
        audio_out=audio_out,
        stt=stt,
        tts=tts,
        emit=emit,
        capture_allowed=lambda: True,
        config=pipeline_config_from(voice_cfg),
    )
    echo("== tts ==")
    t0 = time.perf_counter()
    await pipeline.say(
        TtsRequest(utterance_id="selftest", text="Hallo, ich bin Nox.", language="de")
    )
    echo(f"  spoken in {time.perf_counter() - t0:.2f} s")
    echo(f"== stt: speak now ({seconds:.0f} s) ==")
    await audio_in.start()
    audio_in.enabled = True
    frames: list[np.ndarray] = []
    deadline = time.perf_counter() + seconds
    async for frame in audio_in.frames():
        frames.append(frame)
        if time.perf_counter() >= deadline:
            break
    await audio_in.stop()
    audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
    transcript = await stt.transcribe(audio, audio_in.sample_rate, language=voice_cfg.stt.language)
    echo(
        f"  {transcript.duration_ms} ms audio -> {transcript.latency_ms} ms, "
        f"lang={transcript.language}, "
        f"conf={transcript.confidence:.2f}: {transcript.text!r}"
    )
    await stt.unload()
    await tts.unload()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m nox.worker", description="Nox voice worker")
    ap.add_argument("--service", choices=sorted(SERVICES), default="voice")
    ap.add_argument("--hub-url", default=os.environ.get("NOX_HUB_URL", DEFAULT_HUB_URL))
    ap.add_argument(
        "--selftest", action="store_true", help="devices, models, TTS + 3 s mic; no hub"
    )
    ap.add_argument("--plugin", default=None, help="run as the worker of plugins/<id> (ST-11-01)")
    ap.add_argument("--stt-model", default=None, help="override voice.stt.model (base|small|...)")
    ap.add_argument("--tts-engine", default=None, choices=["piper", "kokoro"])
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.plugin:
        # Plugin mode: no audio devices, no priority change, nothing of the voice path is touched.
        from nox.worker.plugin import run_plugin_worker

        plugin_token = os.environ.get("NOX_WORKER_TOKEN", "")
        if len(plugin_token) < 16:
            typer.echo("NOX_WORKER_TOKEN missing (plugins are spawned by the core)", err=True)
            return 2
        return asyncio.run(run_plugin_worker(args.plugin, args.hub_url, plugin_token))
    voice_cfg = VoiceConfig()
    if args.stt_model:
        voice_cfg = voice_cfg.model_copy(
            update={"stt": voice_cfg.stt.model_copy(update={"model": args.stt_model})}
        )
    if args.tts_engine:
        voice_cfg = voice_cfg.model_copy(
            update={"tts": voice_cfg.tts.model_copy(update={"engine": args.tts_engine})}
        )
    raise_priority()
    if args.selftest:
        return asyncio.run(run_selftest(voice_cfg))
    token = os.environ.get("NOX_WORKER_TOKEN", "")
    if len(token) < 16:
        typer.echo("NOX_WORKER_TOKEN missing (workers are spawned by the core)", err=True)
        return 2
    return asyncio.run(run_worker(args.service, args.hub_url, token, voice_cfg))
