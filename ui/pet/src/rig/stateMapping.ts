/**
 * What the pet is doing, translated into which clips run and how the body is held.
 *
 * `petState.ts` stays the only place that decides *what Nox is*. This file is a projection of that
 * onto a rig, in exactly the same spirit as `spriteExpressionFor` is a projection onto frames — so
 * a state the rig cannot express degrades to a quieter version of itself rather than to something
 * invented.
 *
 * Two layers come out of it. `sustained` clips are the motion: breathing, a tail, a head that
 * turns towards a voice. `posture` is the held shape an emotion has — ears back, chin up, head
 * low — expressed as bone deltas that sit under every clip, because a mood is not an animation and
 * should not loop.
 */

import type { Expression, PetInput } from '../petState';
import type { BoneTransform, Pose } from './types';

/** Clip names this mapping expects a rig to provide. A rig missing one simply never plays it. */
export const RIG_CLIPS = [
  'breathe',
  'sleep_breathe',
  'blink',
  'ear_flick',
  'head_tilt',
  'head_turn',
  'tail_sway',
  'perk',
  'speak_idle',
  'startle',
] as const;
export type RigClipName = (typeof RIG_CLIPS)[number];

export interface SustainedClip {
  name: RigClipName;
  weight: number;
}

/** How much unprompted life the pet may show: full, eyes only, or none at all. */
export type AmbientLevel = 'full' | 'blink-only' | 'none';

export interface RigPlan {
  sustained: readonly SustainedClip[];
  ambient: AmbientLevel;
  posture: Pose;
  /** 0..1 held lid closure under everything, for a sleeping pet whose eyes stay shut. */
  lidClosure: number;
}

function bone(delta: Partial<BoneTransform>): BoneTransform {
  return { rotate: 0, x: 0, y: 0, scaleX: 1, scaleY: 1, ...delta };
}

/**
 * The held shape of each mood, at full intensity. Values are small on purpose: this is a
 * photograph of a real animal being deformed, and anything a real puppy's neck could not do reads
 * immediately as a warping picture rather than as a feeling.
 *
 * Only the moods a body can actually show are listed. The rest are carried by the status label and
 * the capture indicator, exactly as they are for the sprite renderer.
 */
const POSTURE_TABLE: Partial<Record<Expression, Pose>> = {
  happy: { head: bone({ rotate: -1.5 }), 'ear.l': bone({ rotate: -4 }), 'ear.r': bone({ rotate: 4 }) },
  excited: {
    neck: bone({ y: -0.006 }),
    'ear.l': bone({ rotate: -7 }),
    'ear.r': bone({ rotate: 7 }),
  },
  hype: { neck: bone({ y: -0.008 }), 'ear.l': bone({ rotate: -8 }), 'ear.r': bone({ rotate: 8 }) },
  celebrating: { neck: bone({ y: -0.01 }), head: bone({ rotate: -3 }) },
  curious: { head: bone({ rotate: 5 }), 'ear.l': bone({ rotate: -5 }), 'ear.r': bone({ rotate: 3 }) },
  confused: { head: bone({ rotate: 7 }), 'ear.l': bone({ rotate: 4 }), 'ear.r': bone({ rotate: -6 }) },
  bored: {
    neck: bone({ y: 0.008, rotate: 2 }),
    'ear.l': bone({ rotate: 10 }),
    'ear.r': bone({ rotate: -10 }),
  },
  sad: {
    neck: bone({ y: 0.012, rotate: 1 }),
    head: bone({ rotate: 2 }),
    'ear.l': bone({ rotate: 13 }),
    'ear.r': bone({ rotate: -13 }),
  },
  shy: { neck: bone({ y: 0.008 }), head: bone({ rotate: -6 }), 'ear.l': bone({ rotate: 9 }) },
  scared: {
    neck: bone({ y: 0.006 }),
    head: bone({ rotate: -2 }),
    'ear.l': bone({ rotate: 16 }),
    'ear.r': bone({ rotate: -16 }),
  },
  angry: {
    neck: bone({ y: -0.004, rotate: -2 }),
    'ear.l': bone({ rotate: 12 }),
    'ear.r': bone({ rotate: -12 }),
  },
  tilted: { head: bone({ rotate: -8 }), 'ear.l': bone({ rotate: 10 }), 'ear.r': bone({ rotate: -4 }) },
  proud: { neck: bone({ y: -0.007 }), head: bone({ rotate: -1 }) },
  smug: { head: bone({ rotate: -4 }), 'ear.l': bone({ rotate: -3 }) },
  sleeping: { neck: bone({ y: 0.014, rotate: 2 }), 'ear.l': bone({ rotate: 8 }), 'ear.r': bone({ rotate: -8 }) },
};

/** Scale a posture by intensity so a faint mood is a faint shape, not a switch. */
function scalePosture(posture: Pose, intensity: number): Pose {
  const scaled: Record<string, BoneTransform> = {};
  for (const [name, transform] of Object.entries(posture)) {
    scaled[name] = {
      rotate: transform.rotate * intensity,
      x: transform.x * intensity,
      y: transform.y * intensity,
      scaleX: 1 + (transform.scaleX - 1) * intensity,
      scaleY: 1 + (transform.scaleY - 1) * intensity,
    };
  }
  return scaled;
}

/** The pet is asleep, or we have no idea what it is doing — both look the same and should. */
export function isRestingState(input: PetInput): boolean {
  if (!input.connected) return true;
  if (input.functional === 'unavailable') return true;
  return input.expression === 'sleeping' || input.sleep === 'offline';
}

/**
 * The clips and posture for one pet state.
 *
 * Under `prefers-reduced-motion` this collapses to breathing and a held posture: the creature is
 * still alive and still shows its mood, but nothing moves that the viewer did not ask for.
 */
export function planFor(input: PetInput, reducedMotion: boolean): RigPlan {
  const resting = isRestingState(input);
  const intensity = Math.min(1, Math.max(0, input.intensity));
  const posture = scalePosture(POSTURE_TABLE[input.expression as Expression] ?? {}, intensity);

  if (resting) {
    return {
      sustained: [
        { name: 'sleep_breathe', weight: 1 },
        { name: 'tail_sway', weight: reducedMotion ? 0 : 0.25 },
      ],
      ambient: 'none',
      posture,
      lidClosure: 1,
    };
  }

  if (reducedMotion) {
    return { sustained: [{ name: 'breathe', weight: 1 }], ambient: 'none', posture, lidClosure: 0 };
  }

  // Energy decides how much the tail says. A flat tail on a happy pet is the tell that a rig is
  // playing canned loops rather than reacting.
  const energy = Math.min(1, Math.max(0, input.mood.energy));
  const sustained: SustainedClip[] = [
    { name: 'breathe', weight: 1 },
    { name: 'tail_sway', weight: 0.35 + energy * 0.55 },
  ];

  switch (input.functional) {
    case 'listening':
      sustained.push({ name: 'perk', weight: 1 }, { name: 'head_tilt', weight: 0.55 });
      break;
    case 'thinking':
    case 'working':
      sustained.push({ name: 'head_turn', weight: 0.5 }, { name: 'head_tilt', weight: 0.35 });
      break;
    case 'speaking':
      sustained.push({
        name: 'speak_idle',
        weight: 0.35 + Math.min(1, Math.max(0, input.speakingLevel)) * 0.65,
      });
      break;
    default:
      break;
  }

  return {
    sustained,
    ambient: input.functional === 'speaking' ? 'blink-only' : 'full',
    posture,
    lidClosure: 0,
  };
}
