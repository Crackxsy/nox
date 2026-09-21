"""faster-whisper SttEngine on CPU int8 (model size decided by).

Language handling: `auto` restricts detection to the configured languages (DE primary, EN
secondary):
if Whisper's top guess is outside that set the best allowed language is forced in a second pass, so
German with a few English words never comes back as Dutch. Audio never leaves memory.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

from nox.core.events import HealthStatus
from nox.voice._logging import get_logger
from nox.voice.audio import resample_linear
from nox.voice.base import Transcript
from nox.voice.models import engine_models_dir

log = get_logger(__name__)

WHISPER_RATE = 16000


class FasterWhisperStt:
    id = "faster-whisper"

    def __init__(
        self,
        *,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        models_dir: str = "",
        languages: tuple[str, ...] = ("de", "en"),
        cpu_threads: int = 0,
        beam_size: int = 1,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.models_dir = models_dir or str(engine_models_dir("faster-whisper"))
        self.languages = languages
        #: ctranslate2's default under-uses the 7800X3D; physical cores (capped at 8) were
        # 35 % faster than the default and SMT threads (16) brought nothing.
        self.cpu_threads = cpu_threads or max(1, min(8, (os.cpu_count() or 2) // 2))
        self.beam_size = beam_size
        self._model: Any = None
        self.load_time_ms: float = 0.0
        self._error: str = ""

    async def health(self) -> tuple[HealthStatus, str]:
        if self._model is not None:
            return HealthStatus.AVAILABLE, f"{self.model_size}/{self.compute_type} on {self.device}"
        if self._error:
            return HealthStatus.UNAVAILABLE, self._error
        return HealthStatus.UNAVAILABLE, "not loaded"

    def _load_sync(self) -> None:
        from faster_whisper import WhisperModel

        Path(self.models_dir).mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        self._model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
            download_root=self.models_dir,
            cpu_threads=self.cpu_threads,
            local_files_only=False,
        )
        self.load_time_ms = (time.perf_counter() - t0) * 1000.0

    async def load(self) -> None:
        try:
            await asyncio.to_thread(self._load_sync)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            log.error("stt.load_failed", model=self.model_size, error=self._error)
            raise
        log.info("stt.loaded", model=self.model_size, load_ms=round(self.load_time_ms))

    def _transcribe_sync(self, audio: np.ndarray, language: str | None) -> tuple[str, str, float]:
        model = self._model
        if model is None:
            raise RuntimeError("model not loaded")
        segments, info = model.transcribe(
            audio,
            language=language,
            beam_size=self.beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        texts: list[str] = []
        probs: list[float] = []
        for seg in segments:  # generator: decoding happens here
            texts.append(seg.text.strip())
            probs.append(math.exp(min(seg.avg_logprob, 0.0)))
        detected = str(info.language)
        if language is None and detected not in self.languages:
            all_probs = dict(info.all_language_probs or [])
            best = max(self.languages, key=lambda lang: all_probs.get(lang, 0.0))
            log.debug("stt.language_forced", detected=detected, forced=best)
            return self._transcribe_sync(audio, best)
        confidence = float(sum(probs) / len(probs)) if probs else 0.0
        return " ".join(t for t in texts if t), detected, min(max(confidence, 0.0), 1.0)

    async def transcribe(
        self, audio: np.ndarray, sample_rate: int, *, language: str = "auto"
    ) -> Transcript:
        if self._model is None:
            raise RuntimeError("FasterWhisperStt.load() not called")
        pcm = audio.astype(np.float32, copy=False)
        if pcm.ndim > 1:
            pcm = pcm.mean(axis=1).astype(np.float32)
        pcm = resample_linear(pcm, sample_rate, WHISPER_RATE)
        duration_ms = int(round(pcm.size * 1000 / WHISPER_RATE))
        lang: str | None = None if language in ("", "auto") else language
        t0 = time.perf_counter()
        text, detected, confidence = await asyncio.to_thread(self._transcribe_sync, pcm, lang)
        latency_ms = int(round((time.perf_counter() - t0) * 1000))
        log.debug(
            "stt.transcribed", latency_ms=latency_ms, duration_ms=duration_ms, language=detected
        )
        return Transcript(
            text=text,
            language=detected,
            confidence=confidence,
            duration_ms=duration_ms,
            latency_ms=latency_ms,
        )

    async def unload(self) -> None:
        self._model = None
