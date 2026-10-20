/**
 * Switching between the relief creature and a key-pose drawing (curled up asleep, lying down,
 * eating) - the poses the front-view art cannot reach by deformation.
 *
 * A plain cross-dissolve between two different silhouettes reads as a cut. Here the switch is
 * hidden under a squash, the way an animator would hide it: the creature settles (squashes down),
 * the picture changes while it is lowest, and it springs back up as the new shape. Pure and
 * clock-driven, so it can be tested without a canvas.
 */

import { clamp } from './math';

export interface PoseFrameState {
  /** The key pose involved in the current frame, or null when only the relief creature shows. */
  pose: string | null;
  /** Opacity of that key pose over the relief creature, 0..1. */
  poseAlpha: number;
  /** Vertical squash of the whole creature about its ground point: <0 squash, >0 stretch. */
  squash: number;
  /** False only when the pose fully covers the creature, so the relief pass can be skipped. */
  reliefVisible: boolean;
  /** True while a switch is running. */
  busy: boolean;
}

/** Full switch duration, ms. */
export const SWITCH_MS = 640;
/** Under reduced motion: a short dissolve, no squash. */
export const SWITCH_REDUCED_MS = 200;

const smooth = (x: number): number => {
  const t = clamp(x);
  return t * t * (3 - 2 * t);
};

/** Squash curve over a switch (0..1): settle down, hold, spring back with a small overshoot. */
export function switchSquash(k: number): number {
  if (k <= 0 || k >= 1) return 0;
  if (k < 0.3) return -0.085 * smooth(k / 0.3);
  if (k < 0.68) return -0.085;
  const r = (k - 0.68) / 0.32;
  return -0.085 * Math.cos(r * Math.PI * 1.5) * (1 - r);
}

/** How far the picture has changed over a switch (0..1): only while the creature is lowest. */
export const switchMix = (k: number): number => smooth((k - 0.3) / 0.38);

export class PoseSwitch {
  private shown: string | null = null;
  private from: string | null = null;
  private to: string | null = null;
  private startMs = 0;
  private reduced = false;

  /** Whether `pose` is on screen or part of the switch that is running. */
  involves(pose: string): boolean {
    return this.shown === pose || (this.busy && (this.from === pose || this.to === pose));
  }

  get busy(): boolean {
    return this.from !== this.to;
  }

  /** Jump straight to a pose without a transition (first frame, still frames). */
  set(pose: string | null): void {
    this.shown = this.from = this.to = pose;
  }

  /** Advance to `nowMs` towards `wanted` and say what to draw. */
  update(nowMs: number, wanted: string | null, reduced: boolean): PoseFrameState {
    if (!this.busy && wanted !== this.shown) {
      this.from = this.shown;
      // pose to pose goes through the creature itself: one silhouette change at a time
      this.to = this.shown !== null && wanted !== null ? null : wanted;
      this.startMs = nowMs;
      this.reduced = reduced;
    }
    if (!this.busy) {
      return {
        pose: this.shown,
        poseAlpha: this.shown ? 1 : 0,
        squash: 0,
        reliefVisible: this.shown === null,
        busy: false,
      };
    }
    const dur = this.reduced ? SWITCH_REDUCED_MS : SWITCH_MS;
    const k = (nowMs - this.startMs) / dur;
    if (k >= 1) {
      this.shown = this.from = this.to;
      return this.update(nowMs, wanted, reduced);
    }
    const mix = this.reduced ? smooth(k) : switchMix(k);
    const squash = this.reduced ? 0 : switchSquash(k);
    const entering = this.to !== null;
    const pose = entering ? this.to : this.from;
    const alpha = entering ? mix : 1 - mix;
    return { pose, poseAlpha: alpha, squash, reliefVisible: alpha < 1, busy: true };
  }
}
