"""Piper TtsEngine: sentence-wise streaming PCM16 and pre-rendered clips (FR-5.3/5.4, ADR-009,
SP-03).

Synthesis runs in a worker thread per request; each sentence is pushed to the async consumer as
soon as it is rendered so the first audio is heard while later sentences are still being computed.
`prepared_clip` plays `<clips_dir>/<id>.wav` without synthesis (clip ids are validated against a
strict pattern; no path components allowed). Voice models and clips are resolved below the
configured data directory - see `nox.voice.models`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from nox.core.events import HealthStatus
from nox.voice._logging import get_logger
from nox.voice.base import TtsRequest
from nox.voice.models import default_clips_dir, engine_models_dir
from nox.voice.tts.clips import ClipNotFoundError, play_clip
from nox.voice.tts.sentences import split_sentences

log = get_logger(__name__)

__all__ = ["DEFAULT_VOICES", "ClipNotFoundError", "PiperTts"]

DEFAULT_VOICES: dict[str, str] = {"de": "de_DE-thorsten-medium", "en": "en_US-lessac-medium"}
_END = object()


class PiperTts:
    id = "piper"

    def __init__(
        self,
        *,
        voices: dict[str, str] | None = None,
        models_dir: str | Path = "",
        clips_dir: str | Path = "",
        rate: float = 1.0,
        default_language: str = "de",
    ) -> None:
        self.voice_names = dict(voices or DEFAULT_VOICES)
        self.models_dir = Path(models_dir) if models_dir else engine_models_dir("piper")
        self.clips_dir = Path(clips_dir) if clips_dir else default_clips_dir()
        self.rate = rate
        self.default_language = default_language
        self._voices: dict[str, Any] = {}  # voice name -> PiperVoice
        self._sample_rate = 22050
        self._error = ""
        self.load_time_ms = 0.0

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def loaded_voices(self) -> list[str]:
        return list(self._voices)

    async def health(self) -> tuple[HealthStatus, str]:
        if not self._voices:
            return HealthStatus.UNAVAILABLE, self._error or "not loaded"
        missing = [v for v in self.voice_names.values() if v not in self._voices]
        if missing:
            return HealthStatus.LIMITED, f"missing voices: {', '.join(missing)}"
        return HealthStatus.AVAILABLE, f"voices: {', '.join(self._voices)}"

    def _load_sync(self) -> None:
        import time

        from piper import PiperVoice

        t0 = time.perf_counter()
        rates: set[int] = set()
        for lang, name in self.voice_names.items():
            model = self.models_dir / f"{name}.onnx"
            config = self.models_dir / f"{name}.onnx.json"
            if not model.exists() or not config.exists():
                self._error = f"voice model missing: {model}"
                log.warning("tts.voice_missing", language=lang, path=str(model))
                continue
            voice = PiperVoice.load(model, config_path=config, use_cuda=False)
            self._voices[name] = voice
            rates.add(int(voice.config.sample_rate))
        if len(rates) > 1:
            # AudioOutput opens one stream per utterance at `sample_rate`; mixed rates would need a
            # resample per chunk. All *-medium voices are 22050 Hz, so treat this as a config error.
            raise ValueError(f"all Piper voices must share one sample rate, got {sorted(rates)}")
        if rates:
            self._sample_rate = rates.pop()
        self.load_time_ms = (time.perf_counter() - t0) * 1000.0

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)
        if not self._voices:
            raise FileNotFoundError(self._error or "no Piper voices found")
        log.info(
            "tts.loaded",
            engine=self.id,
            voices=list(self._voices),
            load_ms=round(self.load_time_ms),
        )

    async def unload(self) -> None:
        self._voices.clear()

    def _pick_voice(self, request: TtsRequest) -> Any:
        if request.voice and request.voice in self._voices:
            return self._voices[request.voice]
        name = self.voice_names.get(request.language) or self.voice_names.get(self.default_language)
        if name in self._voices:
            return self._voices[name]
        if self._voices:
            return next(iter(self._voices.values()))
        raise RuntimeError("PiperTts.load() not called")

    def _synth_config(self, request: TtsRequest) -> Any:
        from piper import SynthesisConfig

        rate = max(0.25, min(4.0, request.rate * self.rate))
        return SynthesisConfig(length_scale=1.0 / rate, volume=1.0, normalize_audio=True)

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        if request.prepared_clip:
            async for chunk in self._play_clip(request.prepared_clip):
                yield chunk
            return
        voice = self._pick_voice(request)
        config = self._synth_config(request)
        sentences = split_sentences(request.text)
        if not sentences:
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | object] = asyncio.Queue()
        cancel = threading.Event()

        def worker() -> None:
            try:
                for sentence in sentences:
                    if cancel.is_set():
                        break
                    for audio_chunk in voice.synthesize(sentence, config):
                        if cancel.is_set():
                            break
                        loop.call_soon_threadsafe(queue.put_nowait, audio_chunk.audio_int16_bytes)
            except Exception as exc:  # noqa: BLE001 - surfaced to the consumer below
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _END)

        thread = threading.Thread(target=worker, name=f"piper-{request.utterance_id}", daemon=True)
        thread.start()
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    break
                if isinstance(item, Exception):
                    raise item
                assert isinstance(item, bytes)
                yield item
        finally:
            cancel.set()

    async def _play_clip(self, clip_id: str) -> AsyncIterator[bytes]:
        async for chunk in play_clip(clip_id, self.clips_dir, self._sample_rate):
            yield chunk
