/**
 * Clip sampling, additive blending and the player's fades.
 *
 * The property that matters most here is that layered motion stays layered: breathing must not
 * skip when a blink starts on top of it, and a one-shot must leave nothing behind when it ends.
 * The player has no clock of its own, so all of that is checked by passing times in.
 */

import { describe, expect, it } from 'vitest';

import { AmbientScheduler, seededRandom } from '../rig/ambient';
import { accumulate, clipPhase, sampleClip, sampleTrack } from '../rig/clips';
import { RigPlayer } from '../rig/player';
import type { Clip } from '../rig/types';
import { IDENTITY_TRANSFORM } from '../rig/types';

function clip(name: string, overrides: Partial<Clip> = {}): Clip {
  return {
    name,
    durationMs: 1000,
    loop: true,
    easing: 'linear',
    tracks: [
      {
        bone: 'chest',
        channel: 'scaleY',
        keys: [
          { t: 0, value: 1 },
          { t: 0.5, value: 1.1 },
          { t: 1, value: 1 },
        ],
      },
    ],
    ...overrides,
  };
}

function around(value: number, expected: number, tolerance = 1e-6): void {
  expect(Math.abs(value - expected)).toBeLessThan(tolerance);
}

describe('track sampling', () => {
  const keys = [
    { t: 0, value: 0 },
    { t: 0.5, value: 10 },
    { t: 1, value: 0 },
  ];

  it('returns the first and last key outside the keyed range', () => {
    around(sampleTrack(keys, -1, 'linear'), 0);
    around(sampleTrack(keys, 2, 'linear'), 0);
  });

  it('interpolates linearly between keys', () => {
    around(sampleTrack(keys, 0.25, 'linear'), 5);
    around(sampleTrack(keys, 0.75, 'linear'), 5);
  });

  it('uses the easing named on the key it is leaving, not the one it is entering', () => {
    const eased = [
      { t: 0, value: 0, easing: 'step' as const },
      { t: 0.5, value: 10 },
      { t: 1, value: 20 },
    ];
    around(sampleTrack(eased, 0.25, 'linear'), 0);
    around(sampleTrack(eased, 0.75, 'linear'), 15);
  });

  it('falls back to the clip easing when a key names none', () => {
    around(sampleTrack(keys, 0.25, 'step'), 0);
  });
});

describe('clip phase', () => {
  it('wraps a looping clip', () => {
    around(clipPhase(clip('a'), 2500), 0.5);
  });

  it('holds the last frame of a one-shot rather than restarting it', () => {
    around(clipPhase(clip('a', { loop: false }), 2500), 1);
    around(clipPhase(clip('a', { loop: false }), -50), 0);
  });
});

describe('additive blending', () => {
  it('adds rotation and multiplies scale, so two 2 % stretches make 4 % and not 100 %', () => {
    const pose = accumulate({}, { chest: { ...IDENTITY_TRANSFORM, rotate: 3, scaleY: 1.02 } }, 1);
    accumulate(pose, { chest: { ...IDENTITY_TRANSFORM, rotate: 2, scaleY: 1.02 } }, 1);
    around(pose.chest.rotate, 5);
    around(pose.chest.scaleY, 1.0404, 1e-9);
  });

  it('scales a contribution back towards rest by its weight', () => {
    const pose = accumulate({}, { chest: { ...IDENTITY_TRANSFORM, rotate: 10, scaleY: 1.2 } }, 0.5);
    around(pose.chest.rotate, 5);
    around(pose.chest.scaleY, 1.1);
  });

  it('changes nothing at weight zero', () => {
    const pose = accumulate({}, { chest: { ...IDENTITY_TRANSFORM, rotate: 10 } }, 0);
    expect(pose.chest).toBeUndefined();
  });

  it('leaves bones a clip never mentions out of the pose entirely', () => {
    expect(Object.keys(sampleClip(clip('breathe'), 0))).toEqual(['chest']);
  });
});

describe('the player', () => {
  const clips = {
    breathe: clip('breathe'),
    blink: clip('blink', {
      durationMs: 200,
      loop: false,
      tracks: [
        {
          bone: 'lid',
          channel: 'scaleY',
          keys: [
            { t: 0, value: 1 },
            { t: 0.5, value: 0 },
            { t: 1, value: 1 },
          ],
        },
      ],
    }),
  };

  it('reports which clips a rig has, so a caller can see a gap instead of guessing', () => {
    const player = new RigPlayer(clips);
    expect(player.has('breathe')).toBe(true);
    expect(player.has('ear_flick')).toBe(false);
  });

  it('ignores an unknown clip rather than throwing mid-frame', () => {
    const player = new RigPlayer(clips);
    player.play('nonexistent', 0);
    expect(player.running).toEqual([]);
  });

  it('reaches full weight immediately when asked for no fade', () => {
    const player = new RigPlayer(clips);
    player.play('breathe', 0, { fadeMs: 0 });
    player.update(0);
    around(player.weightOf('breathe'), 1);
  });

  it('fades in over the time it was given', () => {
    const player = new RigPlayer(clips);
    player.play('breathe', 0, { fadeMs: 200 });
    player.update(0);
    player.update(100);
    around(player.weightOf('breathe'), 0.5);
    player.update(200);
    around(player.weightOf('breathe'), 1);
  });

  it('keeps a running clip on its own timeline when a second one starts on top', () => {
    const player = new RigPlayer(clips);
    player.play('breathe', 0, { fadeMs: 0 });
    player.update(0);
    const halfway = player.update(500).chest.scaleY;
    player.play('blink', 500, { fadeMs: 0, restart: true });
    const afterBlinkStarted = player.update(500).chest.scaleY;
    around(afterBlinkStarted, halfway);
    expect(player.update(500).lid.scaleY).toBe(1);
  });

  it('removes a one-shot once it has played out, leaving no residue in the pose', () => {
    const player = new RigPlayer(clips);
    player.play('blink', 0, { fadeMs: 0 });
    player.update(0);
    expect(player.update(100).lid.scaleY).toBeLessThan(0.5);
    player.update(250);
    player.update(260);
    expect(player.running).toEqual([]);
    expect(player.update(300).lid).toBeUndefined();
  });

  it('keeps only the clips a new state asks for', () => {
    const player = new RigPlayer(clips);
    player.play('breathe', 0, { fadeMs: 0 });
    player.play('blink', 0, { fadeMs: 0 });
    player.update(0);
    player.keepOnly(['breathe'], 0);
    player.update(10);
    expect(player.running).toEqual(['breathe']);
  });

  it('applies the posture under every clip, and at rest when nothing is playing', () => {
    const player = new RigPlayer(clips);
    player.setPosture({ 'ear.l': { ...IDENTITY_TRANSFORM, rotate: 12 } });
    around(player.update(0)['ear.l'].rotate, 12);
  });

  it('does not replay a hidden hour when the window comes back', () => {
    const player = new RigPlayer(clips);
    player.play('breathe', 0, { fadeMs: 0 });
    player.update(0);
    const before = player.update(250).chest.scaleY;
    player.resetClock(3_600_000);
    around(player.update(3_600_000).chest.scaleY, before);
  });
});

describe('the ambient scheduler', () => {
  const clips = {
    blink: clip('blink', { durationMs: 200, loop: false }),
    ear_flick: clip('ear_flick', { durationMs: 220, loop: false }),
  };

  it('fires nothing before its first gap has passed', () => {
    const player = new RigPlayer(clips);
    const ambient = new AmbientScheduler(() => 0.5);
    ambient.reset(0);
    ambient.update(1000, 'full', player);
    expect(player.running).toEqual([]);
  });

  it('blinks once the gap has passed and then waits again', () => {
    const player = new RigPlayer(clips);
    const ambient = new AmbientScheduler(() => 0);
    ambient.reset(0);
    ambient.update(4300, 'full', player);
    expect(player.running).toContain('blink');
    // Let the one-shot drain, then ask again well before the next gap of 4200 ms is up.
    player.update(4300);
    player.update(4600);
    player.update(4700);
    expect(player.running).toEqual([]);
    ambient.update(6000, 'full', player);
    expect(player.running).not.toContain('blink');
  });

  it('never moves a sleeping pet', () => {
    const player = new RigPlayer(clips);
    const ambient = new AmbientScheduler(() => 0);
    ambient.reset(0);
    ambient.update(100_000, 'none', player);
    expect(player.running).toEqual([]);
  });

  it('blinks but does not flick an ear while the pet is speaking', () => {
    const player = new RigPlayer(clips);
    const ambient = new AmbientScheduler(() => 0);
    ambient.reset(0);
    ambient.update(100_000, 'blink-only', player);
    expect(player.running).toEqual(['blink']);
  });

  it('is reproducible when seeded, so two review renders can be compared', () => {
    const first = seededRandom(7);
    const second = seededRandom(7);
    expect([first(), first(), first()]).toEqual([second(), second(), second()]);
    expect(seededRandom(7)()).not.toEqual(seededRandom(8)());
  });

  it('produces values inside the unit interval', () => {
    const random = seededRandom(0x4e6f78);
    for (let i = 0; i < 500; i += 1) {
      const value = random();
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThan(1);
    }
  });
});
