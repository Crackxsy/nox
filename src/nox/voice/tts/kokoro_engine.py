"""Kokoro (kokoro-onnx) TtsEngine behind the same protocol as Piper (#21, SP-03).

Why it exists: Piper's dependency chain (`piper-phonemize`, espeak-ng) is GPL-3.0, which forces
every bundled Nox build under GPL-3.0 as a whole (see `NOTICE`). Kokoro's own code and the
`kokoro-v1.0` model are permissively licensed, so this engine is the route to an Apache-2.0-only
installer.

Two honest caveats, both surfaced through `health()` rather than hidden:

* Kokoro v1.0 ships no German voice - the voice bank is en-us/en-gb, es, fr, hi, it, ja, pt-br and
  zh. German text is rendered with the default English voice, which sounds like a German sentence
  read by an English speaker. `health()` reports LIMITED whenever a configured language has no
  native voice.
* `kokoro-onnx` still pulls `phonemizer`/espeak-ng (GPL-3.0) for grapheme-to-phoneme conversion, so
  installing the `voice-kokoro` extra does not by itself make a bundled build Apache-2.0-only. The
  model and the ONNX runtime are clean; the text front end is not. This is recorded in
  `docs/license_policy.yaml` and `NOTICE` and is a decision for the product owner.

Model files live in `<data_dir>/models/kokoro/` and are never downloaded automatically: a missing
file is an `unavailable` health reason that names the path and the download command.
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
from nox.voice.models import (
    KOKORO_DOWNLOAD_HINT,
    KOKORO_FILES,
    default_clips_dir,
    engine_models_dir,
    missing_files,
)
from nox.voice.tts.clips import play_clip
from nox.voice.tts.sentences import split_sentences

log = get_logger(__name__)

MODEL_FILES: tuple[str, ...] = tuple(KOKORO_FILES)
KOKORO_SAMPLE_RATE = 24000

#: Language -> the phoneme front end Kokoro should use. Only languages with their own voice bank
#: are listed; everything else falls back to `en-us` (see `SUBSTITUTED_LANGUAGES`).
KOKORO_LANGS: dict[str, str] = {
    "en": "en-us",
    "es": "es",
    "fr": "fr-fr",
    "hi": "hi",
    "it": "it",
    "ja": "ja",
    "pt": "pt-br",
    "zh": "cmn",
}
#: Languages Nox speaks that Kokoro v1.0 has no voice for; rendered with the English default.
SUBSTITUTED_LANGUAGES: frozenset[str] = frozenset({"de"})
#: af_heart is the highest-rated voice in the Kokoro v1.0 voice bank and the most neutral one for
#: German text read by an English voice, so it is the default for both languages.
DEFAULT_VOICES: dict[str, str] = {"de": "af_heart", "en": "af_heart"}

_END = object()


class KokoroTts:
    id = "kokoro"

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
        self.models_dir = Path(models_dir) if models_dir else engine_models_dir("kokoro")
        self.clips_dir = Path(clips_dir) if clips_dir else default_clips_dir()
        self.rate = rate
        self.default_language = default_language
        self._kokoro: Any = None
        self._available_voices: tuple[str, ...] = ()
        self._sample_rate = KOKORO_SAMPLE_RATE
        self._error = ""
        self.load_time_ms = 0.0

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def loaded_voices(self) -> list[str]:
        return [v for v in dict.fromkeys(self.voice_names.values()) if v in self._available_voices]

    def missing_model_files(self) -> list[str]:
        return missing_files(self.models_dir, MODEL_FILES)

    def unavailable_reason(self) -> str:
        """Why the engine cannot run, phrased so the user can act on it."""
        missing = self.missing_model_files()
        if missing:
            return (
                f"kokoro model files missing in {self.models_dir}: "
                f"{', '.join(missing)} - {KOKORO_DOWNLOAD_HINT}"
            )
        return self._error or "not loaded"

    async def health(self) -> tuple[HealthStatus, str]:
        if self._kokoro is None:
            return HealthStatus.UNAVAILABLE, self.unavailable_reason()
        substituted = sorted(set(self.voice_names) & SUBSTITUTED_LANGUAGES)
        if substituted:
            fallback = self.voice_names.get(substituted[0], DEFAULT_VOICES["en"])
            return HealthStatus.LIMITED, (
                f"no native Kokoro v1.0 voice for {', '.join(substituted)}; "
                f"rendered with {fallback} (English)"
            )
        return HealthStatus.AVAILABLE, f"voices: {', '.join(self.loaded_voices)}"

    # ---- loading ---------------------------------------------------------------------------------

    def _load_sync(self) -> None:
        if self.missing_model_files():
            raise FileNotFoundError(self.unavailable_reason())
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            self._error = (
                "kokoro-onnx is not installed - install the `voice-kokoro` extra "
                "(`uv sync --extra voice-kokoro`)"
            )
            raise ImportError(self._error) from exc
        t0 = time.perf_counter()
        kokoro = Kokoro(
            str(self.models_dir / "kokoro-v1.0.onnx"),
            str(self.models_dir / "voices-v1.0.bin"),
        )
        self.load_time_ms = (time.perf_counter() - t0) * 1000.0
        try:
            self._available_voices = tuple(sorted(kokoro.get_voices()))
        except Exception:  # noqa: BLE001 - voice introspection is a nicety, not a requirement
            self._available_voices = tuple(sorted(set(self.voice_names.values())))
        wanted = set(self.voice_names.values())
        unknown = sorted(v for v in wanted if v not in self._available_voices)
        if unknown:
            self._error = (
                f"unknown Kokoro voice(s) {', '.join(unknown)}; "
                f"available: {', '.join(self._available_voices)}"
            )
            raise ValueError(self._error)
        self._kokoro = kokoro

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)
        log.info(
            "tts.loaded",
            engine=self.id,
            voices=self.loaded_voices,
            load_ms=round(self.load_time_ms),
        )

    async def unload(self) -> None:
        self._kokoro = None
        self._available_voices = ()

    # ---- synthesis -------------------------------------------------------------------------------

    def _pick_voice(self, request: TtsRequest) -> str:
        if request.voice:
            return request.voice
        return (
            self.voice_names.get(request.language)
            or self.voice_names.get(self.default_language)
            or DEFAULT_VOICES["en"]
        )

    def _pick_lang(self, language: str) -> str:
        return KOKORO_LANGS.get(language, KOKORO_LANGS["en"])

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        """Yield PCM16 at `sample_rate`, one sentence at a time, synthesized off the event loop."""
        if request.prepared_clip:
            async for chunk in play_clip(request.prepared_clip, self.clips_dir, self._sample_rate):
                yield chunk
            return
        kokoro = self._kokoro
        if kokoro is None:
            raise RuntimeError("KokoroTts.load() not called")
        sentences = split_sentences(request.text)
        if not sentences:
            return
        voice = self._pick_voice(request)
        lang = self._pick_lang(request.language)
        speed = max(0.5, min(2.0, request.rate * self.rate))
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | object] = asyncio.Queue()
        cancel = threading.Event()

        def worker() -> None:
            try:
                for sentence in sentences:
                    if cancel.is_set():
                        break
                    samples, _rate = kokoro.create(sentence, voice=voice, speed=speed, lang=lang)
                    pcm = np.asarray(samples, dtype=np.float32) * 32767.0
                    data = pcm.clip(-32768, 32767).astype(np.int16).tobytes()
                    loop.call_soon_threadsafe(queue.put_nowait, data)
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
