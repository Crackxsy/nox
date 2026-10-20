/**
 * Pet state -> relief body language. Every one of the 21 expressions and every functional state has
 * to come out as something a viewer can see, and the safety rules (reduced motion, privacy,
 * offline, muted) have to hold whatever the mood says.
 */

import { describe, expect, it } from 'vitest';

import {
  DEFAULT_MOOD,
  EXPRESSIONS,
  type Functional,
  type PetInput,
  deriveAnim,
  stillState,
} from '../petState';
import { type ReliefTargets, reliefTargets } from '../relief/stateMapping';

function input(overrides: Partial<PetInput> = {}): PetInput {
  return {
    connected: true,
    functional: 'idle',
    expression: 'normal',
    intensity: 1,
    mood: DEFAULT_MOOD,
    sleep: 'none',
    speakingLevel: 0,
    feeds: 0,
    ...overrides,
  };
}

const targets = (i: PetInput, reduced = false): ReliefTargets => reliefTargets(i, deriveAnim(i), reduced);

const NUMERIC: (keyof ReliefTargets)[] = [
  'lid',
  'lower',
  'asleep',
  'ears',
  'earTwist',
  'tilt',
  'lift',
  'wander',
  'attention',
  'bounce',
  'flutter',
  'wag',
  'sway',
  'tremble',
  'crouch',
  'talk',
  'speech',
  'puff',
  'light',
];
const FUNCTIONALS: Functional[] = [
  'idle',
  'listening',
  'thinking',
  'speaking',
  'working',
  'error',
  'muted',
  'privacy',
  'unavailable',
];

/** How different two target sets look: summed absolute difference over the visible channels. */
function distance(a: ReliefTargets, b: ReliefTargets): number {
  let d = Math.abs(a.gaze[0] - b.gaze[0]) + Math.abs(a.gaze[1] - b.gaze[1]);
  for (const k of NUMERIC) d += Math.abs((a[k] as number) - (b[k] as number));
  return d;
}

describe('relief state mapping', () => {
  it('gives every expression and functional state finite, in-range targets', () => {
    for (const expression of EXPRESSIONS)
      for (const functional of FUNCTIONALS) {
        const t = targets(input({ expression, functional }));
        for (const k of [
          'lid',
          'lower',
          'asleep',
          'wander',
          'attention',
          'bounce',
          'flutter',
          'wag',
          'crouch',
          'talk',
          'speech',
          'puff',
        ] as const) {
          expect(t[k], `${expression}/${functional}.${k}`).toBeGreaterThanOrEqual(0);
          expect(t[k], `${expression}/${functional}.${k}`).toBeLessThanOrEqual(1);
        }
        for (const k of ['ears', 'earTwist', 'tilt', 'lift'] as const)
          expect(Math.abs(t[k])).toBeLessThanOrEqual(1);
      }
  });

  it('makes each of the 21 expressions visibly different from the neutral face', () => {
    const neutral = targets(input());
    for (const expression of EXPRESSIONS) {
      if (expression === 'normal') continue;
      expect(distance(targets(input({ expression })), neutral), expression).toBeGreaterThan(0.15);
    }
  });

  it('scales a mood with its intensity instead of switching it', () => {
    const neutral = targets(input());
    const faint = distance(targets(input({ expression: 'sad', intensity: 0.3 })), neutral);
    const full = distance(targets(input({ expression: 'sad', intensity: 1 })), neutral);
    expect(faint).toBeGreaterThan(0);
    expect(full).toBeGreaterThan(faint * 2);
  });

  it('sleeps with closed eyes when asleep, offline or stopped', () => {
    for (const i of [
      input({ expression: 'sleeping' }),
      input({ connected: false }),
      input({ functional: 'unavailable' }),
      input({ sleep: 'offline' }),
    ]) {
      const t = targets(i);
      expect(t.asleep).toBe(1);
      expect(t.lid).toBe(1);
      expect(t.talk).toBe(0);
      expect(t.ambient).toBe('none');
    }
  });

  it('pricks its ears and faces you when listening', () => {
    const t = targets(input({ functional: 'listening' }));
    expect(t.attention).toBe(1);
    expect(t.ears).toBeLessThan(-0.5);
    expect(t.wander).toBeLessThan(0.3);
  });

  it('looks up and aside when thinking, down when working', () => {
    expect(targets(input({ functional: 'thinking' })).gaze[1]).toBeLessThan(-0.3);
    expect(targets(input({ functional: 'working' })).gaze[1]).toBeGreaterThan(0.3);
  });

  it('moves the jaw only while there is a voice, and never while muted', () => {
    const speaking = targets(input({ functional: 'speaking', speakingLevel: 0.8 }));
    expect(speaking.talk).toBe(1);
    expect(speaking.speech).toBeGreaterThan(0.5);
    expect(targets(input()).talk).toBe(0);
    expect(targets(input({ functional: 'muted', speakingLevel: 0.8 })).talk).toBe(0);
  });

  it('bounces for joy and trembles for fear', () => {
    expect(targets(input({ expression: 'celebrating' })).bounce).toBeGreaterThan(0.3);
    expect(targets(input({ expression: 'scared' })).ears).toBeGreaterThan(0.8);
    expect(targets(input({ expression: 'scared' })).crouch).toBeGreaterThan(0.8);
    expect(targets(input({ expression: 'sad' })).lift).toBeLessThan(-0.5);
  });

  it('shuts its eyes and looks away for privacy, without relying on colour', () => {
    const t = targets(input({ functional: 'privacy' }));
    expect(t.lid).toBeGreaterThan(0.8);
    expect(Math.abs(t.gaze[0])).toBeGreaterThan(0.5);
    expect(t.wander).toBe(0);
  });

  it('under reduced motion keeps the mood but drops every unprompted movement', () => {
    for (const expression of EXPRESSIONS) {
      const t = targets(input({ expression }), true);
      expect(t.wander).toBe(0);
      expect(t.bounce).toBe(0);
      expect(t.wag).toBe(0);
      expect(t.flutter).toBe(0);
      expect(t.tremble).toBe(0);
      expect(t.ambient).toBe('none');
    }
    expect(targets(input({ expression: 'sad' }), true).ears).toBe(targets(input({ expression: 'sad' })).ears);
  });
});

describe('preview states (per-state recordings)', () => {
  it('can show every expression, every functional state and offline', () => {
    for (const e of EXPRESSIONS) expect(stillState(e).expression).toBe(e);
    for (const f of FUNCTIONALS) expect(stillState(f).functional).toBe(f === 'working' ? 'idle' : f);
    expect(stillState('offline').connected).toBe(false);
    expect(stillState('nonsense')).toEqual(stillState('normal'));
  });
});
