/**
 * Which key pose a state asks for.
 *
 * The lying-down pose was unreachable for as long as it existed: the old table was keyed by a
 * string that was either `sleeping` or the functional state, and its `bored` entry named an
 * expression, which that string never was. Art for it shipped and no creature ever lay down. The
 * first test below is the one that would have caught it.
 */

import { describe, expect, it } from 'vitest';

import { DEFAULT_MOOD, type PetInput } from '../petState';
import { EATING_POSE, REACHABLE_POSES, poseFor } from '../rig/stateMapping';

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

describe('poseFor', () => {
  it('lies down when Nox is idle and bored', () => {
    expect(poseFor(input({ expression: 'bored' }))).toBe('lie');
  });

  it('sits up for a voice whatever the mood, because that is what the base picture is', () => {
    expect(poseFor(input({ expression: 'bored', functional: 'listening' }))).toBeNull();
    expect(poseFor(input({ expression: 'bored', functional: 'speaking' }))).toBeNull();
  });

  it('curls up asleep', () => {
    expect(poseFor(input({ expression: 'sleeping' }))).toBe('curl');
    expect(poseFor(input({ sleep: 'offline' }))).toBe('curl');
  });

  it('curls up when the core is gone, because "no idea what it is doing" looks like sleep', () => {
    expect(poseFor(input({ connected: false }))).toBe('curl');
    expect(poseFor(input({ functional: 'unavailable' }))).toBe('curl');
  });

  it('keeps the base drawing for everything else', () => {
    for (const functional of ['idle', 'thinking', 'working', 'error', 'muted', 'privacy'] as const) {
      expect(poseFor(input({ functional })), functional).toBeNull();
    }
  });

  it('only ever asks for poses the reachable set names', () => {
    const asked = new Set<string>([EATING_POSE]);
    for (const expression of ['normal', 'bored', 'sleeping', 'happy']) {
      for (const functional of ['idle', 'listening', 'speaking'] as const) {
        const pose = poseFor(input({ expression, functional }));
        if (pose) asked.add(pose);
      }
    }
    expect([...asked].sort()).toEqual([...REACHABLE_POSES].sort());
  });
});
