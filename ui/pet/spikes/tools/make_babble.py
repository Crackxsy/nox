"""Synthesise a short creature "babble" clip and the amplitude envelope the mouth follows.

The talking part of every prototype is driven by an envelope measured from real audio, the same
way the product would measure Piper output: RMS per animation frame. Synthetic speech avoids any
voice of a real person and keeps the clip license-free.

    uv run --with numpy python tools/make_babble.py
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

SR = 22050
OUT = Path(__file__).resolve().parents[1] / "public"
VOWELS = {  # formant centre frequencies (Hz), scaled up ~1.25x for a small creature
    "a": (1000, 1500, 3100), "e": (600, 2300, 3200), "i": (400, 2800, 3600),
    "o": (650, 1100, 3000), "u": (420, 1000, 2900),
}


def resonator(x: np.ndarray, f: float, bw: float) -> np.ndarray:
    r = np.exp(-np.pi * bw / SR)
    a1, a2 = -2 * r * np.cos(2 * np.pi * f / SR), r * r
    y = np.zeros_like(x)
    for n in range(2, len(x)):
        y[n] = x[n] - a1 * y[n - 1] - a2 * y[n - 2]
    return y * (1 - r)


def main() -> None:
    rng = np.random.default_rng(7)
    total = 3.4
    n = int(total * SR)
    t = np.arange(n) / SR
    env = np.zeros(n)
    vowel_track: list[tuple[int, int, str]] = []
    pos = 0.12
    while pos < total - 0.25:
        if 1.45 < pos < 1.75:  # a breath pause in the middle of the "sentence"
            pos = 1.85
        dur = rng.uniform(0.11, 0.27)
        a, b = int(pos * SR), int(min(total, pos + dur) * SR)
        k = np.arange(b - a) / SR
        shape = np.minimum(1, k / 0.025) * np.exp(-k / (dur * 0.9)) * rng.uniform(0.55, 1.0)
        env[a:b] = np.maximum(env[a:b], shape)
        vowel_track.append((a, b, rng.choice(list(VOWELS))))
        pos += dur + rng.uniform(0.03, 0.11)

    f0 = 300 + 45 * np.sin(2 * np.pi * 0.6 * t) - 40 * t / total  # gentle falling intonation
    phase = np.cumsum(f0 / SR)
    pulses = (np.diff(np.floor(phase), prepend=0) > 0).astype(np.float64)
    src = np.convolve(pulses, np.hanning(24), "same") + 0.03 * rng.normal(size=n)

    voiced = np.zeros(n)
    for a, b, v in vowel_track:
        seg = src[max(0, a - 400): b + 400]
        out = sum(resonator(seg, f * 1.25, 90 + 40 * i) * g
                  for i, (f, g) in enumerate(zip(VOWELS[v], (1.0, 0.6, 0.25))))
        lo = max(0, a - 400)
        voiced[lo: lo + len(out)] += out * np.pad(np.ones(b - a), (a - lo, 400))[: len(out)]
    sig = voiced * env
    sig = 0.6 * sig / np.abs(sig).max()

    # Envelope for the mouth: RMS per 1/60 s, normalised, light smoothing like a VU meter.
    hop = SR // 60
    frames = len(sig) // hop
    rms = np.sqrt(np.array([np.mean(sig[i * hop:(i + 1) * hop] ** 2) for i in range(frames)]))
    rms = rms / rms.max()
    OUT.mkdir(exist_ok=True)
    (OUT / "envelope.json").write_text(json.dumps({"fps": 60, "duration": total,
                                                   "values": [round(float(v), 4) for v in rms]}))
    with wave.open(str(OUT / "babble.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((sig * 32767).astype("<i2").tobytes())
    print(f"{frames} envelope frames, {len(vowel_track)} syllables")


if __name__ == "__main__":
    main()
