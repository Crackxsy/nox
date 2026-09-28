"""The voice commands a user runs by hand: the self-test and the model downloads.

Mounted by the top-level CLI as `nox voice ...` and reachable as `python -m nox.worker
--selftest | --download-kokoro | --download-whisper`. Nothing here talks to the core: a download is
the user's own request, made from their own shell, which is why it does not pass the core's egress
guard - and why no other path in Nox ever fetches a model.

The commands read the same merged configuration the core uses (defaults plus `user.yaml`), so a
self-test checks the devices and models the user actually configured, not the shipped defaults.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

import numpy as np
import typer

from nox.core.config import VoiceConfig
from nox.core.events import E
from nox.voice._logging import get_logger
from nox.voice.base import TtsRequest
from nox.voice.pipeline import DefaultVoicePipeline

log = get_logger(__name__)

#: The host API whose devices the self-test lists on Windows; elsewhere every device is listed.
_WINDOWS_HOSTAPI = "Windows WASAPI"


def local_voice_config() -> VoiceConfig:
    """The `voice` section as the core would load it, or the defaults when that fails."""
    from nox.core.config import load_config
    from nox.paths import resolve_config_paths

    try:
        return load_config(*resolve_config_paths()).voice
    except Exception as exc:  # noqa: BLE001 - a CLI falls back to the defaults and says so
        log.warning("voice.config_unreadable", error=f"{type(exc).__name__}: {exc}")
        return VoiceConfig()


def device_lines(devices: list[dict[str, str]], *, platform: str = sys.platform) -> list[str]:
    """One line per device for the self-test; on Windows only the WASAPI view of each device."""
    lines: list[str] = []
    for dev in devices:
        if platform == "win32" and dev["hostapi"] != _WINDOWS_HOSTAPI:
            continue
        flags = ("IN " if dev["inputs"] != "0" else "   ") + (
            "OUT" if dev["outputs"] != "0" else "   "
        )
        default = (
            " (default)"
            if dev["default_input"] == "True" or dev["default_output"] == "True"
            else ""
        )
        lines.append(f"  [{dev['index']:>2}] {flags} {dev['name']}{default}")
    return lines


async def run_selftest(voice_cfg: VoiceConfig, *, seconds: float = 3.0) -> int:
    """Manual validation: devices, model load, TTS to the default device, 3 s mic transcription."""
    from nox.voice.audio import list_devices
    from nox.worker.main import build_components, build_wake_gate, pipeline_config_from

    echo = typer.echo
    echo("== devices ==")
    for line in device_lines(list_devices()):
        echo(line)
    components = build_components(voice_cfg, service="voice")
    stt, tts = components["stt"], components["tts"]
    audio_in, audio_out = components["audio_in"], components["audio_out"]
    echo("== models ==")
    t0 = time.perf_counter()
    try:
        await stt.load()
    except FileNotFoundError as exc:
        echo(f"  stt: {exc}")
        return 1
    echo(f"  stt {stt.id} {stt.model_size}: {stt.load_time_ms:.0f} ms")
    await tts.load()
    echo(
        f"  tts {tts.id} {tts.loaded_voices}: {tts.load_time_ms:.0f} ms "
        f"(total {time.perf_counter() - t0:.1f} s)"
    )
    for name, engine in (("stt", stt), ("tts", tts)):
        status, reason = await engine.health()
        echo(f"  {name} health: {status} ({reason})")
    gate = build_wake_gate(voice_cfg)
    gate_status, gate_reason = gate.health()
    echo(f"  wake gate: {gate.detector.id} -> {gate_status} ({gate_reason})")
    echo(f"  listening mode: {voice_cfg.stt.listening_mode}")

    async def emit(name: str, payload: dict[str, Any]) -> None:
        echo(f"  event {name} {payload if name != E.VOICE_TRANSCRIPT_READY else payload['text']!r}")

    pipeline = DefaultVoicePipeline(
        audio_in=audio_in,
        audio_out=audio_out,
        stt=stt,
        tts=tts,
        emit=emit,
        capture_allowed=lambda: True,
        config=pipeline_config_from(voice_cfg),
        wake_gate=gate,
    )
    echo("== tts ==")
    t0 = time.perf_counter()
    await pipeline.say(
        TtsRequest(utterance_id="selftest", text="Hallo, ich bin Nox.", language="de")
    )
    echo(f"  spoken in {time.perf_counter() - t0:.2f} s")
    echo(f"== stt: speak now ({seconds:.0f} s) ==")
    await audio_in.start()
    await audio_in.set_enabled(True)
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


def download_whisper_model(voice_cfg: VoiceConfig, name: str = "", *, force: bool = False) -> int:
    """Fetch a Whisper model into `<models_dir>/faster-whisper/<name>`; the configured one by
    default. Returns a process exit code."""
    from nox.voice.models import download_whisper, engine_models_dir

    model = name or voice_cfg.stt.model
    root = engine_models_dir("faster-whisper", voice_cfg.models_dir)
    return download_whisper(root, model, echo=typer.echo, force=force)


#: Mounted by the top-level CLI as `nox voice ...` (`app.add_typer(voice_app, name="voice")`).
voice_app = typer.Typer(help="Voice models and self-test.", no_args_is_help=True)


@voice_app.command("download-kokoro")
def voice_download_kokoro(
    models_dir: str = typer.Option("", help="override voice.models_dir"),
    force: bool = typer.Option(False, "--force", help="re-download even if the files exist"),
) -> None:
    """Download the Kokoro v1.0 TTS model files (~354 MB). Never happens automatically."""
    from nox.voice.models import download_kokoro, engine_models_dir

    root = models_dir or local_voice_config().models_dir
    code = download_kokoro(engine_models_dir("kokoro", root), echo=typer.echo, force=force)
    raise typer.Exit(code)


@voice_app.command("download-whisper")
def voice_download_whisper(
    model: str = typer.Argument("", help="tiny, base, small, ...; default: voice.stt.model"),
    models_dir: str = typer.Option("", help="override voice.models_dir"),
    force: bool = typer.Option(False, "--force", help="re-download even if the model exists"),
) -> None:
    """Download a Whisper speech-recognition model. Never happens automatically."""
    cfg = local_voice_config()
    if models_dir:
        cfg = cfg.model_copy(update={"models_dir": models_dir})
    raise typer.Exit(download_whisper_model(cfg, model, force=force))


@voice_app.command("selftest")
def voice_selftest() -> None:
    """Devices, model load, TTS playback and a 3 s microphone transcription (no hub)."""
    raise typer.Exit(asyncio.run(run_selftest(local_voice_config())))
