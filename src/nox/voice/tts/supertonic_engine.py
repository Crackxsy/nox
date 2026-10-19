"""Supertonic 3 TtsEngine: natural German and English speech on the CPU, no phonemizer.

Why it exists: Piper is fast but sounds mechanical, and Kokoro v1.0 has no German voice.
Supertonic 3 (Supertone Inc.) is a ~99M-parameter flow-matching model shipped as ONNX that speaks
31 languages, German included, with ten preset voices (`F1`..`F5`, `M1`..`M5`). In a comparison of
local German engines it was the most natural and the most intelligible candidate that still runs
well below real time on a CPU, which keeps the GPU free for games and streaming.

Licensing, stated where the engine is chosen:

* The `supertonic` package is MIT and needs only ONNX Runtime, NumPy, soundfile and
  huggingface-hub. It reads raw characters, so unlike Piper and Kokoro there is no espeak-ng /
  GPL-3.0 grapheme-to-phoneme front end in this engine's dependency tree.
* The model weights are BigScience OpenRAIL-M: free, commercial use allowed, but with use-based
  restrictions that pass on to every user - among them, content must be disclosed as machine
  generated, and the model must not be used to impersonate people or give medical advice. See
  `NOTICE`. Nox never downloads the files on its own (`--download-supertonic`).

Synthesis is not frame-streamed: a sentence is rendered as a whole (about 0.4-0.6 s for a short
sentence with 4 CPU threads), and `synthesize` streams sentence by sentence. A streaming speech
pipeline can call `render` per sentence directly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

import numpy as np

from nox.core.events import HealthStatus
from nox.voice._logging import get_logger
from nox.voice.base import TtsRequest
from nox.voice.models import (
    SUPERTONIC_DOWNLOAD_HINT,
    SUPERTONIC_REQUIRED,
    default_clips_dir,
    engine_models_dir,
    missing_files,
)
from nox.voice.tts.clips import play_clip
from nox.voice.tts.sentences import split_sentences
from nox.voice.tts.streaming import stream_in_thread

log = get_logger(__name__)

#: Model name inside the `supertonic` package that matches the pinned files in `nox.voice.models`.
MODEL_NAME = "supertonic-3"
#: Languages Supertonic 3 was trained on (upstream `SUPPORTED_LANGUAGES` at the pinned revision).
#: Anything else is synthesized with the model's "unknown language" token instead of failing.
_LANGUAGE_CODES = "en ko ja ar bg cs da de el es et fi fr hi hr hu id it lt lv nl pl pt ro ru sk sl"
SUPPORTED_LANGUAGES: frozenset[str] = frozenset((*_LANGUAGE_CODES.split(), "sv", "tr", "uk", "vi"))
UNKNOWN_LANGUAGE = "na"
#: F3 had the best automatic naturalness score of the ten voices and the liveliest pitch of the top
#: ones; it is a starting point, not a decision - pick a voice with `voice.tts.voice`.
DEFAULT_VOICES: dict[str, str] = {"de": "F3", "en": "F3"}
#: Flow-matching steps. 5 keeps a short sentence around half a second on 4 CPU threads; upstream's
#: default of 8 is slightly more natural and about 40 % slower.
DEFAULT_STEPS = 5
#: ONNX Runtime threads. Nox shares the machine with games and a stream encoder, so it does not take
#: every core by default.
DEFAULT_THREADS = 4
#: Upstream speaking rate at `rate == 1.0`, and the range the model accepts.
BASE_SPEED = 1.05
MIN_SPEED, MAX_SPEED = 0.7, 2.0


class SupertonicTts:
    id = "supertonic"

    def __init__(
        self,
        *,
        voices: dict[str, str] | None = None,
        models_dir: str | Path = "",
        clips_dir: str | Path = "",
        rate: float = 1.0,
        default_language: str = "de",
        steps: int = DEFAULT_STEPS,
        threads: int = DEFAULT_THREADS,
    ) -> None:
        if steps < 1:
            raise ValueError("steps must be at least 1")
        if threads < 1:
            raise ValueError("threads must be at least 1")
        self.voice_names = dict(voices or DEFAULT_VOICES)
        self.models_dir = Path(models_dir) if models_dir else engine_models_dir("supertonic")
        self.clips_dir = Path(clips_dir) if clips_dir else default_clips_dir()
        self.rate = rate
        self.default_language = default_language
        self.steps = steps
        self.threads = threads
        self._tts: Any = None
        self._styles: dict[str, Any] = {}  # voice name -> supertonic Style
        self._sample_rate = 44100
        self._error = ""
        self.load_time_ms = 0.0

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def loaded_voices(self) -> list[str]:
        return list(self._styles)

    def missing_model_files(self) -> list[str]:
        styles = tuple(f"voice_styles/{v}.json" for v in dict.fromkeys(self.voice_names.values()))
        return missing_files(self.models_dir, SUPERTONIC_REQUIRED + styles)

    def available_voices(self) -> list[str]:
        """Voice names whose style file is present, e.g. `["F1", ..., "M5"]`."""
        directory = self.models_dir / "voice_styles"
        return sorted(p.stem for p in directory.glob("*.json")) if directory.is_dir() else []

    def unavailable_reason(self) -> str:
        """Why the engine cannot run, phrased so the user can act on it."""
        missing = self.missing_model_files()
        if missing:
            return (
                f"supertonic model files missing in {self.models_dir}: "
                f"{', '.join(missing)} - {SUPERTONIC_DOWNLOAD_HINT}"
            )
        return self._error or "not loaded"

    async def health(self) -> tuple[HealthStatus, str]:
        if self._tts is None:
            return HealthStatus.UNAVAILABLE, self.unavailable_reason()
        return HealthStatus.AVAILABLE, (
            f"voices: {', '.join(self.loaded_voices)} "
            f"({self.steps} steps, {self.threads} CPU threads)"
        )

    # ---- loading ---------------------------------------------------------------------------------

    def _load_sync(self) -> None:
        wanted = list(dict.fromkeys(self.voice_names.values()))
        unknown = [v for v in wanted if v not in self.available_voices()]
        missing_core = missing_files(self.models_dir, SUPERTONIC_REQUIRED)
        if missing_core:
            raise FileNotFoundError(self.unavailable_reason())
        if unknown:
            self._error = (
                f"unknown Supertonic voice(s) {', '.join(unknown)}; "
                f"available: {', '.join(self.available_voices()) or 'none'}"
            )
            raise ValueError(self._error)
        try:
            from supertonic import TTS
        except ImportError as exc:
            self._error = (
                "supertonic is not installed - install the `voice-supertonic` extra "
                "(`uv sync --extra voice-supertonic`)"
            )
            raise ImportError(self._error) from exc
        t0 = time.perf_counter()
        tts = TTS(
            model=MODEL_NAME,
            model_dir=self.models_dir,
            auto_download=False,
            intra_op_num_threads=self.threads,
            inter_op_num_threads=1,
        )
        styles = {name: tts.get_voice_style(name) for name in wanted}
        # ONNX Runtime pays a one-off setup cost on the first run (about a second on a desktop
        # CPU); pay it here so the first real sentence is not the slow one.
        tts.synthesize("Hallo.", voice_style=styles[wanted[0]], total_steps=self.steps, lang="de")
        self.load_time_ms = (time.perf_counter() - t0) * 1000.0
        self._sample_rate = int(tts.sample_rate)
        self._styles = styles
        self._tts = tts

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)
        log.info(
            "tts.loaded",
            engine=self.id,
            voices=self.loaded_voices,
            steps=self.steps,
            threads=self.threads,
            load_ms=round(self.load_time_ms),
        )

    async def unload(self) -> None:
        self._tts = None
        self._styles = {}

    # ---- synthesis -------------------------------------------------------------------------------

    def _pick_voice(self, language: str, voice: str = "") -> str:
        if voice in self._styles:
            return voice
        if voice:
            # Only configured voices are loaded; an unknown per-request voice must not silence Nox.
            log.warning("tts.voice_not_loaded", engine=self.id, voice=voice)
        return (
            self.voice_names.get(language)
            or self.voice_names.get(self.default_language)
            or next(iter(self._styles))
        )

    @staticmethod
    def _pick_lang(language: str) -> str:
        return language if language in SUPPORTED_LANGUAGES else UNKNOWN_LANGUAGE

    def _speed(self, rate: float) -> float:
        return max(MIN_SPEED, min(MAX_SPEED, BASE_SPEED * rate * self.rate))

    def _render_sync(self, text: str, *, voice: str, lang: str, speed: float) -> bytes:
        tts = self._tts
        if tts is None:
            raise RuntimeError("SupertonicTts.load() not called")
        wav, _duration = tts.synthesize(
            text,
            voice_style=self._styles[voice],
            total_steps=self.steps,
            speed=speed,
            lang=lang,
        )
        samples = np.asarray(wav, dtype=np.float32).reshape(-1) * 32767.0
        return samples.clip(-32768, 32767).astype(np.int16).tobytes()

    async def render(
        self, sentence: str, *, language: str = "de", voice: str = "", rate: float = 1.0
    ) -> bytes:
        """Synthesize one sentence to PCM16 at `sample_rate`, off the event loop.

        The sentence-level entry point for a streaming speech pipeline: call it per sentence as
        the text arrives. Empty text yields empty audio.
        """
        if not sentence.strip():
            return b""
        if self._tts is None:
            raise RuntimeError("SupertonicTts.load() not called")
        return await asyncio.to_thread(
            self._render_sync,
            sentence.strip(),
            voice=self._pick_voice(language, voice),
            lang=self._pick_lang(language),
            speed=self._speed(rate),
        )

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        """Yield PCM16 at `sample_rate`, one sentence at a time, synthesized off the event loop."""
        if request.prepared_clip:
            async for chunk in play_clip(request.prepared_clip, self.clips_dir, self._sample_rate):
                yield chunk
            return
        if self._tts is None:
            raise RuntimeError("SupertonicTts.load() not called")
        sentences = split_sentences(request.text)
        if not sentences:
            return
        voice = self._pick_voice(request.language, request.voice)
        lang = self._pick_lang(request.language)
        speed = self._speed(request.rate)

        def render(sentence: str) -> bytes:
            return self._render_sync(sentence, voice=voice, lang=lang, speed=speed)

        # `aclosing`: when our consumer stops (barge-in), the inner stream must be closed right
        # away so its worker stops after the current sentence - `async for` alone would leave
        # that to garbage collection.
        stream = stream_in_thread(
            render, sentences, thread_name=f"supertonic-{request.utterance_id}"
        )
        async with aclosing(stream):
            async for chunk in stream:
                yield chunk
