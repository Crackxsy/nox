/**
 * The clip player: several clips running at once, each fading in and out on its own, summed into
 * one pose per frame.
 *
 * The layering this exists for: breathing is always on, a blink rides on top of it, a head turn
 * rides on top of both, and a startle can cut across all three without any of them being stopped
 * and restarted. Each clip keeps its own start time, so a 220 ms ear flick can begin mid-breath
 * and the breath does not skip.
 *
 * Time is passed in, never read: the player has no clock of its own, which is what makes its
 * behaviour testable without waiting for it.
 */

import { accumulate, sampleClip } from './clips';
import type { BoneTransform, Clip, Pose } from './types';

export interface PlayOptions {
  /** Target weight, 0..1. Defaults to 1. */
  weight?: number;
  /** Milliseconds to reach that weight. 0 snaps. */
  fadeMs?: number;
  /** Restart from the beginning even if this clip is already running. */
  restart?: boolean;
  /** Fade out and remove the clip once it has played through. Ignored for looping clips. */
  removeWhenDone?: boolean;
}

interface ActiveClip {
  clip: Clip;
  startMs: number;
  weight: number;
  targetWeight: number;
  fadeRatePerMs: number;
  removeWhenDone: boolean;
}

/** How fast a clip fades when no duration is given: fast enough to feel immediate, not a cut. */
const DEFAULT_FADE_MS = 180;

export class RigPlayer {
  private readonly active = new Map<string, ActiveClip>();
  private lastUpdateMs: number | null = null;
  private readonly clips: Readonly<Record<string, Clip>>;

  /** Bone deltas applied under every clip, for states that are a posture rather than a motion. */
  private posture: Pose = {};

  constructor(clips: Readonly<Record<string, Clip>>) {
    this.clips = clips;
  }

  /** Clip names this rig actually has. Callers ask before playing so a gap is visible, not silent. */
  has(name: string): boolean {
    return name in this.clips;
  }

  get running(): readonly string[] {
    return [...this.active.keys()];
  }

  weightOf(name: string): number {
    return this.active.get(name)?.weight ?? 0;
  }

  /** Start or re-target a clip. Unknown names are ignored — `has` is how a caller checks. */
  play(name: string, nowMs: number, options: PlayOptions = {}): void {
    const clip = this.clips[name];
    if (!clip) return;
    const targetWeight = options.weight ?? 1;
    const fadeMs = options.fadeMs ?? DEFAULT_FADE_MS;
    const existing = this.active.get(name);
    if (existing && !options.restart) {
      existing.targetWeight = targetWeight;
      existing.fadeRatePerMs = fadeMs <= 0 ? Infinity : 1 / fadeMs;
      existing.removeWhenDone = options.removeWhenDone ?? existing.removeWhenDone;
      return;
    }
    this.active.set(name, {
      clip,
      startMs: nowMs,
      weight: fadeMs <= 0 ? targetWeight : (existing?.weight ?? 0),
      targetWeight,
      fadeRatePerMs: fadeMs <= 0 ? Infinity : 1 / fadeMs,
      removeWhenDone: options.removeWhenDone ?? !clip.loop,
    });
  }

  /** Fade a clip out. It is removed once it reaches zero. */
  stop(name: string, fadeMs = DEFAULT_FADE_MS): void {
    const existing = this.active.get(name);
    if (!existing) return;
    if (fadeMs <= 0) {
      this.active.delete(name);
      return;
    }
    existing.targetWeight = 0;
    existing.fadeRatePerMs = 1 / fadeMs;
    existing.removeWhenDone = true;
  }

  /** Fade out everything except the named clips. The usual way to change state. */
  keepOnly(names: readonly string[], fadeMs = DEFAULT_FADE_MS): void {
    const keep = new Set(names);
    for (const name of this.active.keys()) {
      if (!keep.has(name)) this.stop(name, fadeMs);
    }
  }

  setPosture(posture: Pose): void {
    this.posture = posture;
  }

  /**
   * Advance every running clip to `nowMs` and return the summed pose.
   *
   * A clip that has faded to zero, or a one-shot that has played out and faded, is dropped here —
   * so a startle leaves nothing behind once it has settled.
   */
  update(nowMs: number): Pose {
    const deltaMs = this.lastUpdateMs === null ? 0 : Math.max(0, nowMs - this.lastUpdateMs);
    this.lastUpdateMs = nowMs;

    const pose: Record<string, BoneTransform> = {};
    accumulate(pose, this.posture, 1);

    for (const [name, entry] of this.active) {
      const step = entry.fadeRatePerMs === Infinity ? 1 : deltaMs * entry.fadeRatePerMs;
      if (entry.weight < entry.targetWeight) {
        entry.weight = Math.min(entry.targetWeight, entry.weight + step);
      } else if (entry.weight > entry.targetWeight) {
        entry.weight = Math.max(entry.targetWeight, entry.weight - step);
      }
      const elapsedMs = nowMs - entry.startMs;
      if (entry.removeWhenDone && !entry.clip.loop && elapsedMs >= entry.clip.durationMs) {
        entry.targetWeight = 0;
      }
      if (entry.weight <= 0 && entry.targetWeight <= 0) {
        this.active.delete(name);
        continue;
      }
      accumulate(pose, sampleClip(entry.clip, elapsedMs), entry.weight);
    }
    return pose;
  }

  /** Forget the clock, so a paused pet resumes rather than jumping a hidden hour forward. */
  resetClock(nowMs: number): void {
    const shift = this.lastUpdateMs === null ? 0 : nowMs - this.lastUpdateMs;
    for (const entry of this.active.values()) entry.startMs += shift;
    this.lastUpdateMs = nowMs;
  }
}
