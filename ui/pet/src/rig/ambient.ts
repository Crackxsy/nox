/**
 * The unprompted motion: blinks and ear flicks that nobody asked for.
 *
 * This is the difference between a rig and a creature. A pet that only moves when the state
 * machine tells it to reads as a puppet; one that blinks on its own reads as alive. The intervals
 * are jittered because a blink exactly every four seconds reads as a metronome.
 *
 * The clock and the randomness are both injected, so the schedule can be tested without waiting
 * for it and without flaking.
 */

import type { RigPlayer } from './player';
import type { AmbientLevel } from './stateMapping';

/** Blink gap. Dogs blink less often than people; below four seconds it looks nervous. */
export const BLINK_MIN_GAP_MS = 4200;
export const BLINK_EXTRA_GAP_MS = 3600;
/** Ear flick gap, from the brief: one ear, every 6-14 s. */
export const EAR_FLICK_MIN_GAP_MS = 6000;
export const EAR_FLICK_EXTRA_GAP_MS = 8000;

/**
 * A small deterministic generator (mulberry32) for the preview modes.
 *
 * `?still=1` and `?animate=1` exist so a rig can be reviewed, and a review that captures a
 * different blink every run cannot be compared to the last one. The live pet uses `Math.random`.
 */
export function seededRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = Math.imul(state ^ (state >>> 15), 1 | state);
    value = (value + Math.imul(value ^ (value >>> 7), 61 | value)) ^ value;
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

export class AmbientScheduler {
  private blinkAtMs = 0;
  private earFlickAtMs = 0;

  constructor(private readonly random: () => number = Math.random) {}

  /** Arm both timers relative to `nowMs`. Called on start and whenever the pet wakes up. */
  reset(nowMs: number): void {
    this.blinkAtMs = nowMs + BLINK_MIN_GAP_MS + this.random() * BLINK_EXTRA_GAP_MS;
    this.earFlickAtMs = nowMs + EAR_FLICK_MIN_GAP_MS + this.random() * EAR_FLICK_EXTRA_GAP_MS;
  }

  /**
   * Fire whatever is due. `blink` always beats `ear_flick` to the punch when both come up at once:
   * two involuntary motions in the same frame read as a glitch.
   */
  update(nowMs: number, level: AmbientLevel, player: RigPlayer): void {
    if (level === 'none') return;
    if (nowMs >= this.blinkAtMs) {
      this.blinkAtMs = nowMs + BLINK_MIN_GAP_MS + this.random() * BLINK_EXTRA_GAP_MS;
      if (player.has('blink')) player.play('blink', nowMs, { fadeMs: 0, restart: true });
      return;
    }
    if (level !== 'full') return;
    if (nowMs >= this.earFlickAtMs) {
      this.earFlickAtMs = nowMs + EAR_FLICK_MIN_GAP_MS + this.random() * EAR_FLICK_EXTRA_GAP_MS;
      // One ear, not both: a puppy flicks the ear the sound came from.
      const clip = this.random() < 0.5 ? 'ear_flick' : 'ear_flick_r';
      const chosen = player.has(clip) ? clip : 'ear_flick';
      if (player.has(chosen)) player.play(chosen, nowMs, { fadeMs: 0, restart: true });
    }
  }
}
