/**
 * The jaw's envelope while Nox speaks.
 *
 * What reaches the pet about speech is deliberately thin (`src/nox/ipc/pet_view.py`): that an
 * utterance started and ended, and per `tts.chunk` how many characters were said - never the text,
 * and no audio. `petState.ts` folds that into `speakingLevel`, 0..1. A jaw that simply followed
 * that level would hang open for a whole sentence, so the envelope here synthesises syllables:
 * open-close bumps at a speaking rate, grouped into words with short pauses, their height scaled by
 * the level. It is seeded, so a preview render is the same every time.
 *
 * If the core ever sends a real amplitude (RMS per frame), `sample()` takes it as `measured` and
 * follows it instead; the synthetic syllables are only the stand-in for a signal we do not have.
 */

import { approach, clamp, mulberry32 } from './math';

/** Mean syllable length in seconds (~5 syllables per second, conversational German/English). */
const SYLLABLE_S = 0.19;

export class SpeechEnvelope {
  private rng: () => number;
  private readonly seed: number;
  private nextAt = 0;
  private peakAt = 0;
  private peak = 0;
  private dur = SYLLABLE_S;
  private left = 0;
  private value = 0;

  constructor(seed = 7) {
    this.seed = seed;
    this.rng = mulberry32(seed);
  }

  reset(): void {
    this.rng = mulberry32(this.seed);
    this.nextAt = 0;
    this.peakAt = 0;
    this.peak = 0;
    this.left = 0;
    this.value = 0;
  }

  /**
   * Jaw opening 0..1 at time `t` (seconds). `level` is the speaking level (0 = silent); `measured`
   * a real amplitude when one exists.
   */
  sample(t: number, dt: number, level: number, measured?: number): number {
    let raw: number;
    if (measured !== undefined) {
      raw = clamp(measured);
    } else if (level <= 0.001) {
      raw = 0;
      this.nextAt = Math.min(this.nextAt, t);
    } else {
      if (t >= this.nextAt) this.startSyllable(t, level);
      // attack-decay bump: quick open, slower close, the shape of a voiced syllable
      const s = (t - this.peakAt) / this.dur;
      const bump = s < -0.35 ? 0 : s < 0 ? 1 - (s / -0.35) ** 2 : Math.exp(-s * 2.6);
      raw = this.peak * bump;
    }
    // fast attack, softer release - a jaw does not snap shut
    const tau = raw > this.value ? 0.025 : 0.07;
    this.value += (raw - this.value) * approach(dt, tau);
    return clamp(this.value);
  }

  private startSyllable(t: number, level: number): void {
    const r = this.rng;
    if (this.left <= 0) {
      // a new word: 1-4 syllables, then a short gap
      this.left = 1 + Math.floor(r() * 4);
      if (t > 0 && this.nextAt > 0) {
        this.nextAt = t + 0.08 + r() * 0.16;
        this.peak = 0;
        return;
      }
    }
    this.left -= 1;
    this.dur = SYLLABLE_S * (0.7 + r() * 0.6);
    this.peakAt = t + 0.35 * this.dur;
    this.peak = clamp(level) * (0.55 + r() * 0.45);
    this.nextAt = t + this.dur;
  }
}
