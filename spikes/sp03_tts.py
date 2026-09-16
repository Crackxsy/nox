"""SP-03 TTS engine: Piper (thorsten-medium / lessac-medium) vs Kokoro (kokoro-onnx, EN only).

Measures load time, time-to-first-audio (TTFA: first PCM chunk available to the output) and
real-time factor (synthesis wall time / audio duration) for a 2-sentence text, 5 runs per engine
and language. WAVs land in spikes/out/ for the subjective quality note.
Usage: .venv/Scripts/python.exe spikes/sp03_tts.py [--runs 5] [--no-kokoro]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import wave
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nox.voice.base import TtsRequest  # noqa: E402
from nox.voice.tts.piper_engine import PiperTts  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
TEXT = {
    "de": (
        "Hallo, ich bin Nox, dein Begleiter für Stream und Coding. "
        "Sag mir einfach, was du brauchst, und ich kümmere mich darum."
    ),
    "en": (
        "Hi, I am Nox, your companion for streaming and coding. "
        "Just tell me what you need and I will take care of it."
    ),
}


def save_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)


async def bench(engine: Any, lang: str, runs: int) -> dict[str, object]:
    ttfa: list[float] = []
    total: list[float] = []
    pcm_all = b""
    for i in range(runs):
        req = TtsRequest(utterance_id=f"sp03-{lang}-{i}", text=TEXT[lang], language=lang)
        t0 = time.perf_counter()
        first: float | None = None
        chunks: list[bytes] = []
        async for chunk in engine.synthesize(req):
            if first is None:
                first = time.perf_counter() - t0
            chunks.append(chunk)
        total.append(time.perf_counter() - t0)
        ttfa.append(first or 0.0)
        pcm_all = b"".join(chunks)
    audio_s = len(pcm_all) / 2 / engine.sample_rate
    save_wav(OUT / f"sp03_{engine.id}_{lang}.wav", pcm_all, engine.sample_rate)
    return {
        "engine": engine.id,
        "language": lang,
        "load_ms": round(engine.load_time_ms),
        "audio_s": round(audio_s, 2),
        "ttfa_ms": [round(t * 1000) for t in ttfa],
        "ttfa_median_ms": round(statistics.median(ttfa) * 1000),
        "total_median_ms": round(statistics.median(total) * 1000),
        "rtf": round(statistics.median(total) / audio_s, 3) if audio_s else None,
        "sample_rate": engine.sample_rate,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--no-kokoro", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    results = []
    piper = PiperTts()
    await piper.load()
    for lang in ("de", "en"):
        r = await bench(piper, lang, args.runs)
        results.append(r)
        print(
            f"| piper | {lang} | load {r['load_ms']} ms | TTFA {r['ttfa_median_ms']} ms | "
            f"total {r['total_median_ms']} ms | audio {r['audio_s']} s | RTF {r['rtf']} |",
            flush=True,
        )
    await piper.unload()
    if not args.no_kokoro:
        try:
            from nox.voice.tts.kokoro_engine import KokoroTts

            kokoro = KokoroTts()
            await kokoro.load()
            for lang in ("en", "de"):  # de = German text through the English voice (no DE support)
                r = await bench(kokoro, lang, args.runs)
                results.append(r)
                print(
                    f"| kokoro | {lang} | load {r['load_ms']} ms | TTFA {r['ttfa_median_ms']} ms | "
                    f"total {r['total_median_ms']} ms | audio {r['audio_s']} s | RTF {r['rtf']} |",
                    flush=True,
                )
            await kokoro.unload()
        except Exception as exc:  # noqa: BLE001 - spike: report, do not crash
            print(f"kokoro unavailable: {type(exc).__name__}: {exc}")
            results.append({"engine": "kokoro", "error": f"{type(exc).__name__}: {exc}"})
    (OUT / "sp03_results.json").write_text(
        json.dumps({"text": TEXT, "results": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
