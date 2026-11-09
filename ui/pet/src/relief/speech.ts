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
 * Where the core sends a real amplitude (`tts.level`, the RMS of what is playing), `sample()` takes
 * it as `measured` and follows it instead; `VoiceTracker` below decides when that signal is
 * trusted. The synthetic syllables are only the stand-in for a signal we do not have.
 */

import { approach, clamp, mulberry32 } from './math';

/**
 * Voice RMS at which the jaw is fully open. Speech peaks around 0.2-0.3 RMS at full scale; quieter
 * syllables open it less (a gentle curve, so whispers still move it).
 */
export const FULL_OPEN_RMS = 0.22;

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
      raw = clamp(Math.pow(clamp(measured) / FULL_OPEN_RMS, 0.8));
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

/**
 * No `tts.level` for this long and the voice counts as paused: the jaw eases shut. The core sends
 * a level only when it changes (about 30 a second while a voice is heard, one `0` for silence), so
 * real speech is never this quiet on the wire; a pause between words or sentences is.
 */
export const VOICE_HOLD_MS = 160;

/**
 * A shut jaw opens only for a level at least this loud (RMS). In a pause the output keeps
 * reporting its noise floor now and then; those keepalives sit below this and leave it shut.
 */
export const VOICE_OPEN_RMS = 0.02;

/** An open jaw follows the voice down to this level (RMS); below it the voice has gone quiet. */
export const VOICE_SILENT_RMS = 0.008;

/**
 * Turns the reducer's `voiceLevel` + `voiceSeq` (pure state, no clock) into the level the jaw
 * should follow, or `null` while no level has arrived during this utterance (then the envelope
 * synthesises syllables).
 *
 * Once a level has arrived the measured voice owns the jaw until the utterance ends: a pause is a
 * shut mouth, never the synthetic syllables flapping in. Within it the jaw is gated:
 * - no level for `VOICE_HOLD_MS` (a pause, a stalled connection, a dropped `tts.finished`): 0;
 * - a shut jaw stays shut until a level of at least `VOICE_OPEN_RMS` arrives, so a sparse
 *   low keepalive cannot blip it open;
 * - an open jaw follows the level until it drops below `VOICE_SILENT_RMS` (hysteresis, so a
 *   voice hovering around one threshold does not chatter).
 *
 * The result is a target, not a position: `SpeechEnvelope` eases the jaw towards it (fast attack,
 * softer release), so closing in a pause is a movement, not a snap. Time comes in as `nowMs`
 * with every call, which keeps this deterministic under test.
 */
export class VoiceTracker {
  private seq: number | undefined;
  private at = Number.NEGATIVE_INFINITY;
  private level = 0;
  private measured = false;
  private open = false;

  /**
   * @param nowMs the frame time in milliseconds (any monotonic clock).
   * @param seq `voiceSeq`: changes with every `tts.level` that arrived.
   * @param level `voiceLevel`: the last level that arrived, RMS 0..1.
   * @param speaking whether an utterance is being voiced at all; when it ends, the next one starts
   *   unmeasured again (a core or node that sends no levels gets the synthetic syllables).
   */
  sample(
    nowMs: number,
    seq: number | undefined,
    level: number | undefined,
    speaking: boolean,
  ): number | null {
    if (seq !== undefined && seq !== this.seq) {
      // The first value seen is old news: it arrived before this view started watching.
      if (this.seq !== undefined) {
        this.at = nowMs;
        this.level = clamp(level ?? 0);
        this.measured = true;
      }
      this.seq = seq;
    }
    if (!speaking) {
      this.measured = false;
      this.open = false;
      return null;
    }
    if (!this.measured) return null;
    if (nowMs - this.at >= VOICE_HOLD_MS) {
      this.open = false;
    } else if (this.open) {
      this.open = this.level >= VOICE_SILENT_RMS;
    } else {
      this.open = this.level >= VOICE_OPEN_RMS;
    }
    return this.open ? this.level : 0;
  }
}
