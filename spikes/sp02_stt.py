"""SP-02 STT latency: faster-whisper base vs small, CPU int8, one 3 s German utterance, 5 runs each.

Usage: .venv/Scripts/python.exe spikes/sp02_stt.py [--record] [--runs 5]
       [--models base,small]
Without --record the utterance is synthesized with Piper (de_DE-thorsten-medium) and resampled to
16 kHz; with --record 3 s are captured from the default microphone (kept in memory only).
Results: spikes/out/sp02_results.json (git-ignored) and a markdown table on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nox.voice.audio import resample_linear  # noqa: E402
from nox.voice.base import TtsRequest  # noqa: E402
from nox.voice.stt.faster_whisper_engine import FasterWhisperStt  # noqa: E402
from nox.voice.tts.piper_engine import PiperTts  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
TEXT_DE = "Nox, wie spät ist es und wie wird das Wetter morgen in Witten?"


async def synthesize_utterance() -> tuple[np.ndarray, int]:
    tts = PiperTts()
    await tts.load()
    pcm = b"".join(
        [
            c
            async for c in tts.synthesize(
                TtsRequest(utterance_id="sp02", text=TEXT_DE, language="de")
            )
        ]
    )
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    await tts.unload()
    return resample_linear(audio, tts.sample_rate, 16000), 16000


async def record_utterance(seconds: float = 3.0) -> tuple[np.ndarray, int]:
    from nox.voice.audio import SoundDeviceInput

    mic = SoundDeviceInput()
    await mic.start()
    mic.enabled = True
    print(f"speak now ({seconds:.0f} s): '{TEXT_DE}'", flush=True)
    frames: list[np.ndarray] = []
    deadline = time.perf_counter() + seconds
    async for frame in mic.frames():
        frames.append(frame)
        if time.perf_counter() >= deadline:
            break
    await mic.stop()
    return np.concatenate(frames), 16000


async def bench(
    model: str, audio: np.ndarray, rate: int, runs: int, beam: int, threads: int
) -> dict[str, object]:
    stt = FasterWhisperStt(model_size=model, beam_size=beam, cpu_threads=threads)
    await stt.load()
    latencies: list[int] = []
    texts: list[str] = []
    langs: list[str] = []
    confs: list[float] = []
    for _ in range(runs):
        t = await stt.transcribe(audio, rate, language="auto")
        latencies.append(t.latency_ms)
        texts.append(t.text)
        langs.append(t.language)
        confs.append(t.confidence)
    await stt.unload()
    return {
        "model": model,
        "beam_size": beam,
        "cpu_threads": threads,
        "load_ms": round(stt.load_time_ms),
        "audio_ms": int(audio.size * 1000 / rate),
        "latency_ms": latencies,
        "latency_median_ms": statistics.median(latencies),
        "latency_min_ms": min(latencies),
        "latency_max_ms": max(latencies),
        "rtf": round(statistics.median(latencies) / (audio.size * 1000 / rate), 3),
        "text": texts[0],
        "language": langs[0],
        "confidence": round(statistics.mean(confs), 3),
        "stable": len(set(texts)) == 1,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--models", default="base,small")
    ap.add_argument("--beams", default="1,5")
    ap.add_argument(
        "--threads", type=int, default=0, help="ctranslate2 cpu_threads (0 = library default)"
    )
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    audio, rate = await (record_utterance() if args.record else synthesize_utterance())
    print(
        f"utterance: {audio.size / rate:.2f} s @ {rate} Hz, "
        f"source={'mic' if args.record else 'piper'}"
    )
    results = []
    for model in args.models.split(","):
        for beam in (int(b) for b in args.beams.split(",")):
            r = await bench(model, audio, rate, args.runs, beam, args.threads)
            results.append(r)
            print(
                f"| {model} | beam {beam} | threads {args.threads} | load {r['load_ms']} ms | "
                f"median {r['latency_median_ms']:.0f} ms "
                f"(min {r['latency_min_ms']}, max {r['latency_max_ms']}) | RTF {r['rtf']} | "
                f"{r['language']} {r['confidence']} | {r['text']!r} |",
                flush=True,
            )
    suffix = f"_t{args.threads}" if args.threads else ""
    (OUT / f"sp02_results{suffix}.json").write_text(
        json.dumps(
            {"reference": TEXT_DE, "source": "mic" if args.record else "piper", "results": results},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
