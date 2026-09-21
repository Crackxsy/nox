/**
 * Keyframed clips: easing curves, sampling a track, and sampling a whole clip into a pose delta.
 *
 * A clip is always expressed as a *delta from rest* — rotate/x/y at 0 and the scales at 1 mean
 * "nothing happens". That is what lets several clips be added on top of one another without any of
 * them knowing about the others: breathing does not have to know the head is also turning.
 */

import type { BoneTransform, Channel, Clip, EasingName, Keyframe, Pose, Track } from './types';
import { IDENTITY_TRANSFORM } from './types';

export const EASINGS: Readonly<Record<EasingName, (t: number) => number>> = {
  linear: (t) => t,
  /** The curve almost everything in this rig uses: no corner at either end, so nothing ticks. */
  sine: (t) => 0.5 - Math.cos(Math.PI * t) / 2,
  easeIn: (t) => t * t * t,
  easeOut: (t) => 1 - Math.pow(1 - t, 3),
  easeInOut: (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  /** Holds each key until the next one. The only way to get a snap out of this system. */
  step: () => 0,
};

export const EASING_NAMES = Object.keys(EASINGS) as readonly EasingName[];

export function isEasingName(value: string): value is EasingName {
  return value in EASINGS;
}

/** Value of one track at `t` (0..1 within the clip), with the key's own easing into the next key. */
export function sampleTrack(keys: readonly Keyframe[], t: number, fallback: EasingName): number {
  const last = keys.length - 1;
  if (t <= keys[0].t) return keys[0].value;
  if (t >= keys[last].t) return keys[last].value;
  let index = 0;
  while (index < last && keys[index + 1].t <= t) index += 1;
  const from = keys[index];
  const to = keys[index + 1];
  const span = to.t - from.t;
  const local = span <= 0 ? 1 : (t - from.t) / span;
  const eased = EASINGS[from.easing ?? fallback](local);
  return from.value + (to.value - from.value) * eased;
}

/** Where a clip is at `elapsedMs`, as 0..1. Looping wraps; a one-shot holds its last frame. */
export function clipPhase(clip: Clip, elapsedMs: number): number {
  if (clip.durationMs <= 0) return 1;
  const raw = elapsedMs / clip.durationMs;
  if (!clip.loop) return Math.min(1, Math.max(0, raw));
  return raw - Math.floor(raw);
}

const CHANNEL_REST: Readonly<Record<Channel, number>> = {
  rotate: 0,
  x: 0,
  y: 0,
  scaleX: 1,
  scaleY: 1,
};

/** Sample a clip into a pose delta. Bones the clip never mentions are simply absent. */
export function sampleClip(clip: Clip, elapsedMs: number): Pose {
  const phase = clipPhase(clip, elapsedMs);
  const pose: Record<string, BoneTransform> = {};
  for (const track of clip.tracks) {
    const current = pose[track.bone] ?? { ...IDENTITY_TRANSFORM };
    current[track.channel] = sampleTrack(track.keys, phase, clip.easing);
    pose[track.bone] = current;
  }
  return pose;
}

/**
 * Add one weighted pose delta onto an accumulator.
 *
 * Rotation and translation add; scale multiplies, because two clips that each stretch the chest by
 * 2 % should stretch it by 4 %, not by 100 %. The weight interpolates each contribution back
 * towards rest, so a clip at weight 0 changes nothing at all.
 */
export function accumulate(
  into: Record<string, BoneTransform>,
  delta: Pose,
  weight: number,
): Record<string, BoneTransform> {
  if (weight === 0) return into;
  for (const [bone, transform] of Object.entries(delta)) {
    const current = into[bone] ?? { ...IDENTITY_TRANSFORM };
    into[bone] = {
      rotate: current.rotate + transform.rotate * weight,
      x: current.x + transform.x * weight,
      y: current.y + transform.y * weight,
      scaleX: current.scaleX * (1 + (transform.scaleX - 1) * weight),
      scaleY: current.scaleY * (1 + (transform.scaleY - 1) * weight),
    };
  }
  return into;
}

/** The value a channel has when nothing is driving it. Exported so validation can check keys. */
export function restValue(channel: Channel): number {
  return CHANNEL_REST[channel];
}

/** Does this track ever leave rest? A track that does not is dead weight in the rig file. */
export function trackIsFlat(track: Track): boolean {
  const rest = restValue(track.channel);
  return track.keys.every((key) => key.value === rest);
}
