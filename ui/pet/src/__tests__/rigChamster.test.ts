/**
 * The shipped Chamster rig, checked as data.
 *
 * The same two silent failures the Meereswolf test guards against apply here, and one more that
 * this variant was born from: its art and its skeleton came from different places. The poses were
 * drawn first and sat in the Meereswolf's folder, where the base drawing is a wolf - sleeping
 * cross-dissolved a wolf into a hamster. They belong to this creature, and the checks below are
 * what say so: the base drawing, the eye layers and the three poses all have to describe one
 * animal with one skeleton.
 *
 * The landmarks are read off the photograph in `rig/base.png`, which is why they are stated as
 * normalised image coordinates rather than derived from the bones - deriving them from the thing
 * under test would prove nothing.
 */

import { existsSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import rigJson from '../../public/variants/chamster/rig.json';
import { buildSkeleton } from '../rig/bones';
import { computeWeights } from '../rig/mesh';
import { parseRigDefinition } from '../rig/rigFile';
import { RIG_CLIPS } from '../rig/stateMapping';

const rig = parseRigDefinition(rigJson, 'chamster');
const skeleton = buildSkeleton(rig.bones);

/** Points read off the photograph, as normalised image coordinates. */
const LANDMARKS: Record<string, [number, number]> = {
  eye: [0.6057, 0.4511],
  nose: [0.73, 0.52],
  earTip: [0.5, 0.285],
  chest: [0.6, 0.65],
  tailSpiral: [0.2, 0.72],
  frontPaw: [0.7, 0.95],
};

function weightsAt(point: [number, number]): Record<string, number> {
  const row = computeWeights(skeleton, new Float32Array(point));
  return Object.fromEntries(rig.bones.map((bone, index) => [bone.name, row[index]]));
}

describe('the Chamster rig file', () => {
  it('names every bone the state mapping and the layers expect', () => {
    const names = new Set(rig.bones.map((bone) => bone.name));
    for (const expected of ['root', 'body', 'chest', 'neck', 'head', 'muzzle', 'ear.l', 'ear.r', 'tail']) {
      expect(names).toContain(expected);
    }
    for (const layer of rig.layers) {
      if (layer.lid) expect(names).toContain(layer.lid.bone);
    }
  });

  it('has no fins, because this creature has none', () => {
    // The skeleton was derived from the Meereswolf's, and the wolf's gill fins came with it. A
    // bone with nothing under it is not harmless: `perk` moved them, so the clip would have been
    // rotating a patch of hamster flank.
    const names = rig.bones.map((bone) => bone.name);
    expect(names).not.toContain('fin.l');
    expect(names).not.toContain('fin.r');
    for (const clip of Object.values(rig.clips)) {
      for (const track of clip.tracks) expect(names).toContain(track.bone);
    }
  });

  it('provides every clip the pet states ask for', () => {
    for (const clip of RIG_CLIPS) expect(Object.keys(rig.clips)).toContain(clip);
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

  it('lets each body part be moved by its own bone rather than by the motionless root', () => {
    const owners: Record<string, string> = {
      eye: 'head',
      nose: 'muzzle',
      earTip: 'ear.l',
      chest: 'chest',
      tailSpiral: 'tail',
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

describe('the key poses the variant ships', () => {
  it('names art for the poses the creature actually reaches', () => {
    // `curl` is what sleeping asks for and `lie` is what boredom asks for (RiggedPet.tsx), and
    // `eat` is the treat. This variant is the one that has all three drawn.
    expect(Object.keys(rig.poses).sort()).toEqual(['curl', 'eat', 'lie']);
  });

  it('points every pose at a file that is really there', () => {
    for (const [name, pose] of Object.entries(rig.poses)) {
      const file = join(process.cwd(), 'public', 'variants', 'chamster', rig.assetDir, pose.file);
      expect(existsSync(file), `${name} -> ${pose.file}`).toBe(true);
    }
  });
});
