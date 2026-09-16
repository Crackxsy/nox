"""Kokoro (kokoro-onnx) TtsEngine behind the same protocol as Piper, for the SP-03 comparison.

Kokoro v1.0 has no German voice (en-us, en-gb, ja, zh, es, fr, hi, it, pt-br); German requests are
rendered with the English voice and health reports LIMITED. Streaming is sentence-wise like Piper.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np

from nox.core.events import HealthStatus
from nox.voice._logging import get_logger
from nox.voice.base import TtsRequest
from nox.voice.tts.piper_engine import ClipNotFoundError, chunk_bytes, read_clip_pcm16
from nox.voice.tts.sentences import split_sentences

log = get_logger(__name__)

DEFAULT_MODELS_DIR = r"E:\Nox\models\kokoro"
DEFAULT_CLIPS_DIR = r"E:\Nox\data\clips"
KOKORO_LANGS: dict[str, str] = {"en": "en-us"}  # DE unsupported by Kokoro v1.0
DEFAULT_VOICES: dict[str, str] = {"en": "am_adam", "de": "am_adam"}
_END = object()


class KokoroTts:
    id = "kokoro"

    def __init__(
        self,
        *,
        voices: dict[str, str] | None = None,
        models_dir: str = DEFAULT_MODELS_DIR,
        clips_dir: str = DEFAULT_CLIPS_DIR,
        rate: float = 1.0,
    ) -> None:
        self.voice_names = dict(voices or DEFAULT_VOICES)
        self.models_dir = Path(models_dir)
        self.clips_dir = Path(clips_dir)
        self.rate = rate
        self._kokoro: Any = None
        self._sample_rate = 24000
        self._error = ""
        self.load_time_ms = 0.0

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def health(self) -> tuple[HealthStatus, str]:
        if self._kokoro is None:
            return HealthStatus.UNAVAILABLE, self._error or "not loaded"
        return HealthStatus.LIMITED, "no German voice (Kokoro v1.0 is EN/JA/ZH/ES/FR/HI/IT/PT)"

    def _load_sync(self) -> None:
        from kokoro_onnx import Kokoro
        from kokoro_onnx.config import SAMPLE_RATE

        model = self.models_dir / "kokoro-v1.0.onnx"
        voices = self.models_dir / "voices-v1.0.bin"
        if not model.exists() or not voices.exists():
            self._error = f"kokoro model missing in {self.models_dir}"
            raise FileNotFoundError(self._error)
        t0 = time.perf_counter()
        self._kokoro = Kokoro(str(model), str(voices))
        self._sample_rate = int(SAMPLE_RATE)
        self.load_time_ms = (time.perf_counter() - t0) * 1000.0

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)
        log.info("tts.loaded", engine=self.id, load_ms=round(self.load_time_ms))

    async def unload(self) -> None:
        self._kokoro = None

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        if request.prepared_clip:
            path = self.clips_dir / f"{request.prepared_clip}.wav"
            if "/" in request.prepared_clip or "\\" in request.prepared_clip or not path.is_file():
                raise ClipNotFoundError(str(path))
            pcm = await asyncio.to_thread(read_clip_pcm16, path, self._sample_rate)
            for chunk in chunk_bytes(pcm, self._sample_rate // 10 * 2):
                yield chunk
            return
        kokoro = self._kokoro
        if kokoro is None:
            raise RuntimeError("KokoroTts.load() not called")
        voice = request.voice or self.voice_names.get(request.language) or DEFAULT_VOICES["en"]
        lang = KOKORO_LANGS.get(request.language, "en-us")
        speed = max(0.5, min(2.0, request.rate * self.rate))
        sentences = split_sentences(request.text)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | object] = asyncio.Queue()
        cancel = threading.Event()

        def worker() -> None:
            try:
                for sentence in sentences:
                    if cancel.is_set():
                        break
                    samples, _rate = kokoro.create(sentence, voice=voice, speed=speed, lang=lang)
                    pcm = (np.asarray(samples, dtype=np.float32) * 32767.0).clip(-32768, 32767)
                    loop.call_soon_threadsafe(queue.put_nowait, pcm.astype(np.int16).tobytes())
            except Exception as exc:  # noqa: BLE001 - surfaced to the consumer below
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _END)

        threading.Thread(target=worker, name=f"kokoro-{request.utterance_id}", daemon=True).start()
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
