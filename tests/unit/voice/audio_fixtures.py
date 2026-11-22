"""Synthetic audio for the voice-input tests: speech-like, music-like and noisy signals.

Everything is generated from a seed, so there is no recording of any person in the repository and
nothing to license. `speech_like` is a source-filter model - a pulse train with a wandering pitch,
shaped by two resonances (formants) that change every syllable - which is exactly what a speech
detector listens for; music, game and television sound are built from plain tones and noise.
"""

from __future__ import annotations

import numpy as np

SR = 16000

#: First two formants (Hz) of six vowel-like sounds.
_VOWELS = ((730, 1090), (270, 2290), (300, 870), (660, 1720), (530, 1840), (570, 840))


def _resonator_response(centre_hz: float, bandwidth_hz: float, length: int = 600) -> np.ndarray:
    """Impulse response of a two-pole resonator (decays to nothing well within `length`)."""
    radius = np.exp(-np.pi * bandwidth_hz / SR)
    theta = 2 * np.pi * centre_hz / SR
    n = np.arange(length)
    return np.asarray(radius**n * np.sin((n + 1) * theta) / np.sin(theta))


def speech_like(
    seconds: float, *, seed: int = 0, f0: float = 120.0, amp: float = 0.3
) -> np.ndarray:
    """A voice-like signal: voiced pulses with vibrato, two formants, a syllable rhythm."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    pitch = f0 * (1 + 0.03 * np.sin(2 * np.pi * 3 * t))
    phase = np.cumsum(pitch / SR)
    source = np.diff((phase % 1.0) * 2 - 1, prepend=0.0)
    out = np.zeros(n)
    syllable = int(0.18 * SR)
    for start in range(0, n, syllable):
        f1, f2 = _VOWELS[int(rng.integers(len(_VOWELS)))]
        chunk = source[start : start + syllable]
        shaped = np.convolve(chunk, _resonator_response(f1, 80), mode="full")[: chunk.size]
        shaped = np.convolve(shaped, _resonator_response(f2, 100), mode="full")[: chunk.size]
        out[start : start + chunk.size] = shaped * np.hanning(chunk.size) ** 0.5
    out *= amp / (np.max(np.abs(out)) + 1e-9)
    return out.astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def hiss(seconds: float, *, amp: float = 0.05, seed: int = 1) -> np.ndarray:
    """White noise: a fan, a bad microphone, a television between stations."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * amp).astype(np.float32)


def music_like(seconds: float, *, amp: float = 0.3, seed: int = 2) -> np.ndarray:
    """Sustained chords of harmonic tones with a soft beat: loud, tonal, not speech."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    chord_len = int(0.5 * SR)
    roots = (196.0, 233.1, 261.6, 220.0)
    for index, start in enumerate(range(0, n, chord_len)):
        root = roots[index % len(roots)]
        segment = t[start : start + chord_len]
        chord = np.zeros(segment.size)
        for ratio in (1.0, 1.26, 1.5):
            for harmonic in range(1, 6):
                chord += np.sin(2 * np.pi * root * ratio * harmonic * segment) / harmonic
        out[start : start + segment.size] = chord * np.hanning(segment.size) ** 0.3
    beat = np.exp(-((t % 0.5) * 18.0)) * np.sin(2 * np.pi * 55.0 * t)
    out = out / (np.max(np.abs(out)) + 1e-9) * 0.7 + 0.5 * beat
    out += rng.standard_normal(n) * 0.005
    out *= amp / (np.max(np.abs(out)) + 1e-9)
    return out.astype(np.float32)


def game_like(seconds: float, *, amp: float = 0.4, seed: int = 3) -> np.ndarray:
    """Engine hum plus decaying noise bursts: explosions and gunfire, nothing like a voice."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = 0.3 * np.sin(2 * np.pi * 80 * t) + 0.1 * rng.standard_normal(n)
    for onset in np.arange(0.2, seconds, 0.7):
        start = int(onset * SR)
        burst = rng.standard_normal(min(int(0.3 * SR), n - start))
        out[start : start + burst.size] += burst * np.exp(-np.arange(burst.size) / (0.06 * SR))
    out *= amp / (np.max(np.abs(out)) + 1e-9)
    return out.astype(np.float32)


def frames(signal: np.ndarray, *, frame_ms: int = 30) -> list[np.ndarray]:
    size = SR * frame_ms // 1000
    return [signal[i * size : (i + 1) * size] for i in range(signal.size // size)]


def scaled_to_db(signal: np.ndarray, rms_db: float) -> np.ndarray:
    """`signal` scaled so that its overall RMS level is `rms_db` dBFS."""
    rms = float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))
    return (signal * (10 ** (rms_db / 20) / (rms + 1e-12))).astype(np.float32)
