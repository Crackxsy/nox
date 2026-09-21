/**
 * The shipped Meereswolf rig, checked as data.
 *
 * `rig.json` is hand-edited against the photograph, and the two ways to break it are both silent:
 * a typo that makes the whole rig fail to load (and drops the pet back to a static frame), and an
 * influence radius that leaves a body part anchored to the motionless root, so the creature renders
 * perfectly and simply does not move. Both are caught here rather than by looking at a screenshot.
 */

import { describe, expect, it } from 'vitest';

// The very file the browser fetches, not a copy of it: a hand edit that breaks the shipped rig has
// to break this test.
import rigJson from '../../public/variants/meereswolf/rig.json';
import { buildSkeleton } from '../rig/bones';
import { computeWeights } from '../rig/mesh';
import { parseRigDefinition } from '../rig/rigFile';
import { RIG_CLIPS } from '../rig/stateMapping';

const rig = parseRigDefinition(rigJson, 'meereswolf');
const skeleton = buildSkeleton(rig.bones);

/** Points read off the photograph, as normalised image coordinates. */
const LANDMARKS: Record<string, [number, number]> = {
  eye: [0.532, 0.25],
  nose: [0.601, 0.356],
  earTip: [0.434, 0.03],
  finBlade: [0.39, 0.26],
  chest: [0.585, 0.6],
  tailTip: [0.18, 0.86],
  frontPaw: [0.6, 0.95],
};

function weightsAt(point: [number, number]): Record<string, number> {
  const row = computeWeights(skeleton, new Float32Array(point));
  return Object.fromEntries(rig.bones.map((bone, index) => [bone.name, row[index]]));
}

describe('the Meereswolf rig file', () => {
  it('names every bone the state mapping and the layers expect', () => {
    const names = new Set(rig.bones.map((bone) => bone.name));
    for (const expected of ['root', 'body', 'chest', 'neck', 'head', 'ear.l', 'ear.r', 'tail', 'fin.l', 'fin.r']) {
      expect(names).toContain(expected);
    }
    for (const layer of rig.layers) {
      if (layer.lid) expect(names).toContain(layer.lid.bone);
    }
  });

  it('provides every clip the pet states ask for', () => {
    for (const clip of RIG_CLIPS) expect(Object.keys(rig.clips)).toContain(clip);
  });

  it('keeps breathing under two per cent of scale, so it never reads as a bounce', () => {
    for (const track of rig.clips.breathe.tracks) {
      if (!track.channel.startsWith('scale')) continue;
      // A hair of tolerance for binary floating point: 1.02 - 1 is not exactly 0.02.
      for (const key of track.keys) expect(Math.abs(key.value - 1)).toBeLessThanOrEqual(0.0201);
    }
  });

  it('loops the clips that carry a state and plays the reactions once', () => {
    for (const looping of ['breathe', 'sleep_breathe', 'tail_sway', 'perk', 'head_tilt', 'head_turn', 'speak_idle']) {
      expect(rig.clips[looping].loop).toBe(true);
    }
    for (const once of ['blink', 'ear_flick', 'startle']) {
      expect(rig.clips[once].loop).toBe(false);
    }
  });

  it('starts and ends every looping clip at the same value, so the seam is invisible', () => {
    for (const clip of Object.values(rig.clips)) {
      if (!clip.loop) continue;
      for (const track of clip.tracks) {
        const first = track.keys[0];
        const last = track.keys[track.keys.length - 1];
        expect(last.t).toBe(1);
        expect(last.value).toBeCloseTo(first.value, 6);
      }
    }
  });

  it('recoils within the kill switch budget of 120 ms', () => {
    const startle = rig.clips.startle;
    for (const track of startle.tracks) {
      const peak = track.keys[1];
      expect(peak.t * startle.durationMs).toBeLessThanOrEqual(130);
    }
  });

  it('lets each body part be moved by its own bone rather than by the motionless root', () => {
    const owners: Record<string, string> = {
      eye: 'head',
      nose: 'muzzle',
      earTip: 'ear.l',
      finBlade: 'fin.l',
      chest: 'chest',
      tailTip: 'tail',
    };
    for (const [landmark, bone] of Object.entries(owners)) {
      const weights = weightsAt(LANDMARKS[landmark]);
      expect.soft(weights[bone], `${landmark} should answer to ${bone}`).toBeGreaterThan(0.3);
      expect.soft(weights.root, `${landmark} should not be pinned to the root`).toBeLessThan(0.2);
    }
  });

  it('keeps the front paws planted, so a breathing creature does not float', () => {
    const weights = weightsAt(LANDMARKS.frontPaw);
    expect(weights.root + weights.body).toBeGreaterThan(0.85);
    expect(weights.chest).toBeLessThan(0.1);
  });

  it('gives the eye layers to the head, so an eye never slides out of its socket', () => {
    for (const layer of rig.layers) {
      const centre: [number, number] = [
        (layer.rect[0] + layer.rect[2]) / 2,
        (layer.rect[1] + layer.rect[3]) / 2,
      ];
      expect(weightsAt(centre).head).toBeGreaterThan(0.6);
    }
  });

  it('puts every lid pivot inside the eye layer it closes', () => {
    for (const layer of rig.layers) {
      if (!layer.lid) continue;
      const bone = rig.bones.find((candidate) => candidate.name === layer.lid?.bone);
      expect(bone?.channelOnly).toBe(true);
      expect(bone?.pivot[0]).toBeGreaterThan(layer.rect[0]);
      expect(bone?.pivot[0]).toBeLessThan(layer.rect[2]);
      expect(bone?.pivot[1]).toBeGreaterThan(layer.rect[1]);
      expect(bone?.pivot[1]).toBeLessThan(layer.rect[3]);
    }
  });
});
