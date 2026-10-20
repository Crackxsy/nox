/**
 * What the pet's state means for the relief creature's body, as plain numbers.
 *
 * `petState.ts` stays the only place that decides what Nox *is*; `deriveAnim` already turns the
 * 21 expressions, the functional state, intensity and mood into eye openness, tilt, bob, jitter and
 * so on. This file projects that onto the relief rig's channels and adds the few things a body can
 * say that a parameter list cannot (ears back, chin up, a crouch). Every value is a *target*: the
 * brain eases towards it, so a state change is a movement, never a jump.
 */

import type { AnimParams, Expression, PetInput } from '../petState';
import { isRestingState } from '../rig/stateMapping';
import { clamp } from './math';

export type Ambient = 'full' | 'blink-only' | 'none';

export interface ReliefTargets {
  /** Held upper-lid closure, 0 open .. 1 shut. */
  lid: number;
  /** Lower lid ("smiling eyes"), 0..1. */
  lower: number;
  /** 0 awake .. 1 asleep: head sinks, breath slows, ears droop. */
  asleep: number;
  /** -1 ears pricked forward .. 1 flat back. */
  ears: number;
  /** Ear asymmetry (confused, curious): one up, one down. */
  earTwist: number;
  /** Head roll, -1..1 (sign = direction). */
  tilt: number;
  /** Chin up (+) or head low (-), -1..1. */
  lift: number;
  /** Preferred gaze/head direction, -1..1 each (x right, y down). */
  gaze: [number, number];
  /** 0..1 how much the creature looks around on its own (saccades, head turns). */
  wander: number;
  /** 0..1 how firmly it looks at the viewer (listening, speaking). */
  attention: number;
  /** 0..1 height of the happy bounce; 0 = none. */
  bounce: number;
  /** Bounces per second. */
  bounceRate: number;
  /** 0..1 wing flutter. */
  flutter: number;
  /** 0..1 tail wag strength, on top of the idle sway. */
  wag: number;
  /** 0..1 idle tail sway. */
  sway: number;
  breathRate: number;
  breathAmp: number;
  /** 0..1 trembling (stress). */
  tremble: number;
  /** 0..1 body pulled in and down (scared, shy, sad). */
  crouch: number;
  /** 0..1 the jaw follows the speech envelope. */
  talk: number;
  /** Speaking level for the envelope, 0..1. */
  speech: number;
  /** 0..1 lifts the body and opens the stance (proud, hype). */
  puff: number;
  /** Unprompted life allowed: blinks, glances, ear twitches. */
  ambient: Ambient;
  /** Brightness multiplier (privacy, offline are dimmer). */
  light: number;
}

interface BodyShape {
  ears?: number;
  earTwist?: number;
  lift?: number;
  crouch?: number;
  flutter?: number;
  wag?: number;
  puff?: number;
  gaze?: [number, number];
  wander?: number;
}

/**
 * The body language of each expression at full intensity. Small on purpose: this is a painting
 * being deformed, and anything a real animal's neck could not do reads as a warping picture.
 */
const BODY: Partial<Record<Expression, BodyShape>> = {
  happy: { ears: -0.3, lift: 0.15, wag: 0.6, flutter: 0.15 },
  excited: { ears: -0.6, lift: 0.3, wag: 0.9, flutter: 0.5, wander: 0.4 },
  confused: { earTwist: 0.8, ears: 0.15 },
  curious: { ears: -0.5, earTwist: 0.35, lift: 0.1, gaze: [0.35, -0.1], wander: 0.5 },
  bored: { ears: 0.45, lift: -0.45, wander: -0.6 },
  proud: { lift: 0.55, ears: -0.2, puff: 0.8, wag: 0.25 },
  shy: { ears: 0.55, lift: -0.4, crouch: 0.5, gaze: [-0.45, 0.35], wander: -0.5 },
  scared: { ears: 1, crouch: 1, lift: -0.25, wander: 0.6 },
  angry: { ears: 0.85, lift: -0.15, puff: 0.6 },
  sad: { ears: 0.9, lift: -0.7, crouch: 0.35, gaze: [0, 0.45], wander: -0.6 },
  coding: { gaze: [0, 0.5], wander: -0.4 },
  working: { gaze: [0.1, 0.45], wander: -0.3 },
  thinking: { gaze: [-0.45, -0.45], earTwist: 0.2 },
  coaching: { ears: -0.4, lift: 0.25, wag: 0.3 },
  streaming: { ears: -0.3, lift: 0.15, wag: 0.4, flutter: 0.1 },
  celebrating: { ears: -0.5, lift: 0.4, wag: 1, flutter: 1, wander: 0.3 },
  sleeping: {},
  tilted: { ears: 0.7, earTwist: 0.4, puff: 0.4 },
  hype: { ears: -0.7, lift: 0.4, wag: 1, flutter: 0.9, puff: 0.5, wander: 0.4 },
  smug: { lift: 0.35, ears: -0.1, gaze: [0.3, 0] },
};

/** The relief channels for one pet state. `reduced` = `prefers-reduced-motion`. */
export function reliefTargets(input: PetInput, params: AnimParams, reduced: boolean): ReliefTargets {
  const intensity = clamp(input.intensity);
  const shape = BODY[input.expression as Expression] ?? {};
  const s = (v: number | undefined): number => (v ?? 0) * intensity;
  const energy = clamp(input.mood.energy);
  const resting = isRestingState(input);

  const t: ReliefTargets = {
    lid: clamp((0.85 - params.eyeOpen) / 0.85),
    lower: clamp((params.mouthCurve - 0.15) / 0.85) * 0.8,
    asleep: resting ? 1 : 0,
    ears: clamp(s(shape.ears), -1, 1),
    earTwist: clamp(s(shape.earTwist), -1, 1),
    tilt: clamp(params.tilt * 1.6, -1, 1),
    lift: clamp(s(shape.lift), -1, 1),
    gaze: [clamp(s(shape.gaze?.[0]), -1, 1), clamp(s(shape.gaze?.[1]) - params.eyeLift * 0.9, -1, 1)],
    wander: clamp(0.6 + s(shape.wander)),
    attention: 0,
    bounce: clamp(params.bob),
    bounceRate: 1.6 + 0.8 * energy,
    flutter: clamp(s(shape.flutter)),
    wag: clamp(s(shape.wag) * (0.5 + 0.5 * energy)),
    sway: 0.35 + 0.55 * energy,
    breathRate: params.breathRate,
    breathAmp: params.breathAmp,
    tremble: clamp(params.jitter),
    crouch: clamp(s(shape.crouch)),
    talk: 0,
    speech: 0,
    puff: clamp(s(shape.puff)),
    ambient: 'full',
    light: 1,
  };

  switch (input.functional) {
    case 'listening':
      t.attention = 1;
      t.ears = Math.min(t.ears, -0.7);
      t.wander = 0.15;
      t.tilt = t.tilt || 0.25;
      break;
    case 'thinking':
      t.gaze = [t.gaze[0] || -0.45, Math.min(t.gaze[1], -0.4)];
      t.wander = 0.35;
      break;
    case 'working':
      t.gaze = [t.gaze[0] || 0.1, Math.max(t.gaze[1], 0.45)];
      t.wander = 0.25;
      break;
    case 'speaking':
      t.attention = 0.7;
      t.wander = 0.25;
      t.talk = 1;
      t.speech = clamp(Math.max(input.speakingLevel, 0.35));
      t.ambient = 'blink-only';
      break;
    case 'muted':
      t.talk = 0;
      t.ears = Math.max(t.ears, 0.3);
      break;
    case 'error':
      t.ears = Math.max(t.ears, 0.8);
      t.lift = Math.min(t.lift, -0.3);
      t.crouch = Math.max(t.crouch, 0.3);
      break;
    case 'privacy':
      // eyes nearly shut and the face turned aside: "not looking", readable without colour
      t.gaze = [-0.8, 0.2];
      t.wander = 0;
      t.light = 0.85;
      t.ambient = 'none';
      break;
    default:
      break;
  }
  // a voice that is still trailing off after the state moved on keeps the jaw honest
  if (input.functional !== 'speaking' && input.functional !== 'muted' && input.speakingLevel > 0) {
    t.talk = 1;
    t.speech = clamp(input.speakingLevel);
  }

  if (resting) {
    t.lid = 1;
    t.lower = 0;
    t.ears = 0.5;
    t.wander = 0;
    t.attention = 0;
    t.bounce = 0;
    t.flutter = 0;
    t.wag = 0;
    t.sway = 0.2;
    t.talk = 0;
    t.tremble = 0;
    t.ambient = 'none';
    t.light = input.connected ? 1 : 0.8;
  }

  if (reduced) {
    // still alive and still showing the mood, but nothing moves that the viewer did not ask for
    t.wander = 0;
    t.bounce = 0;
    t.flutter = 0;
    t.wag = 0;
    t.sway = 0;
    t.tremble = 0;
    t.breathAmp *= 0.5;
    t.ambient = 'none';
  }
  return t;
}

/** The key pose (full-body art) the state asks for, or null for the relief creature itself. */
export { poseFor, EATING_POSE } from '../rig/stateMapping';
