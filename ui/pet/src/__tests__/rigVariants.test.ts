/**
 * Every rigged variant, checked against the rules that hold for all of them.
 *
 * There are seven creatures now, and the per-creature tests next to this one check what only their
 * own anatomy can answer - that the Meereswolf's fin bone owns its fin, that the Chamster's tail
 * bone owns its spiral. Everything else is the same question asked seven times, and asking it by
 * hand is how the eighth variant ships broken: these are the failures that produce no error at
 * all, just a pet that stands still, or blinks a hole in its own face, or snaps to a different
 * size when it lies down.
 *
 * The list is read off the directory rather than written here on purpose. A new variant is a new
 * folder, and it must not be possible to add one and quietly stay untested.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { buildSkeleton, poseMatrices } from '../rig/bones';
import { buildGrid, computeWeights, skin } from '../rig/mesh';
import { parseRigDefinition } from '../rig/rigFile';
import { REACHABLE_POSES, RIG_CLIPS } from '../rig/stateMapping';

const VARIANTS_DIR = join(process.cwd(), 'public', 'variants');

/** Folders that ship a `rig.json`; a sprite-only variant is not a failure and is skipped. */
const RIGGED = readdirSync(VARIANTS_DIR).filter((name) =>
  existsSync(join(VARIANTS_DIR, name, 'rig.json')),
);

function load(variant: string) {
  const raw: unknown = JSON.parse(readFileSync(join(VARIANTS_DIR, variant, 'rig.json'), 'utf-8'));
  return parseRigDefinition(raw, variant);
}

it('finds the rigged variants rather than trusting a list in the test', () => {
  // If this ever reads zero, every assertion below passes vacuously and the suite goes green
  // while checking nothing at all.
  expect(RIGGED.length).toBeGreaterThanOrEqual(2);
});

describe.each(RIGGED)('%s', (variant) => {
  const rig = load(variant);
  const names = new Set(rig.bones.map((bone) => bone.name));
  const assets = join(VARIANTS_DIR, variant, rig.assetDir);

  it('names itself the way its folder does', () => {
    // The id is what the loader reports in a fallback warning. One that disagrees with the folder
    // sends whoever reads that log to the wrong file.
    expect(rig.id).toBe(variant);
  });

  it('has the base drawing and every layer texture it names', () => {
    expect(existsSync(join(assets, rig.base)), rig.base).toBe(true);
    for (const layer of rig.layers) {
      expect(existsSync(join(assets, layer.file)), layer.file).toBe(true);
    }
  });

  it('moves only bones it actually has', () => {
    // A track naming a missing bone makes the loader reject the whole rig, and the pet drops to a
    // static frame with nothing on screen to say why. This is the check that the shared clip
    // library was filtered for this creature's skeleton.
    for (const [clipName, clip] of Object.entries(rig.clips)) {
      for (const track of clip.tracks) {
        expect(names, `${clipName} -> ${track.bone}`).toContain(track.bone);
      }
    }
  });

  it('provides the clips the pet states ask for, or does without one it cannot have', () => {
    // `tail_sway` is the exception worth allowing: a koala has no tail, and inventing a bone for
    // one would be worse than the missing clip. Everything else has to be there.
    const missing = RIG_CLIPS.filter((clip) => !(clip in rig.clips));
    expect(missing.every((clip) => clip === 'tail_sway')).toBe(true);
    if (missing.includes('tail_sway')) expect(names.has('tail')).toBe(false);
  });

  it('closes every eyelid onto the eye it belongs to', () => {
    for (const layer of rig.layers) {
      if (!layer.lid) continue;
      const bone = rig.bones.find((candidate) => candidate.name === layer.lid?.bone);
      expect(bone, layer.lid.bone).toBeDefined();
      expect(bone?.channelOnly, `${layer.lid.bone} must not deform the mesh`).toBe(true);
      // Outside its own rect the lid shuts onto a line that is not in the eye, which reads as the
      // eyelid closing somewhere on the cheek.
      expect(bone?.pivot[0]).toBeGreaterThan(layer.rect[0]);
      expect(bone?.pivot[0]).toBeLessThan(layer.rect[2]);
      expect(bone?.pivot[1]).toBeGreaterThan(layer.rect[1]);
      expect(bone?.pivot[1]).toBeLessThan(layer.rect[3]);
    }
  });

  it('gives the eyes to the head, so they travel with it', () => {
    const skeleton = buildSkeleton(rig.bones);
    const head = rig.bones.findIndex((bone) => bone.name === 'head');
    for (const layer of rig.layers) {
      const centre = new Float32Array([
        (layer.rect[0] + layer.rect[2]) / 2,
        (layer.rect[1] + layer.rect[3]) / 2,
      ]);
      const weights = computeWeights(skeleton, centre);
      expect(weights[head], `${layer.name} should answer to the head`).toBeGreaterThan(0.6);
    }
  });

  it('points every key pose at art that exists, and at a pose the pet can reach', () => {
    // A pose nothing asks for is art that never shows; a misspelt one - `curled` for `curl` - is
    // worse, because the loader accepts it and the creature simply never lies down.
    for (const [pose, { file }] of Object.entries(rig.poses)) {
      expect(existsSync(join(assets, file)), `${pose} -> ${file}`).toBe(true);
      expect(REACHABLE_POSES, `${pose} is art nothing ever shows`).toContain(pose);
    }
  });

  it('starts and ends every looping clip at the same value', () => {
    for (const [clipName, clip] of Object.entries(rig.clips)) {
      if (!clip.loop) continue;
      for (const track of clip.tracks) {
        const first = track.keys[0];
        const last = track.keys[track.keys.length - 1];
        expect(last.t, `${clipName}.${track.bone}`).toBe(1);
        expect(last.value, `${clipName}.${track.bone}`).toBeCloseTo(first.value, 6);
      }
    }
  });

  it('keeps every bone inside the picture', () => {
    // A pivot outside the texture is always a measuring mistake, and the part it drives swings
    // around a point off screen instead of bending.
    for (const bone of rig.bones) {
      expect(bone.pivot[0], `${bone.name} x`).toBeGreaterThanOrEqual(0);
      expect(bone.pivot[0], `${bone.name} x`).toBeLessThanOrEqual(1);
      expect(bone.pivot[1], `${bone.name} y`).toBeGreaterThanOrEqual(0);
      expect(bone.pivot[1], `${bone.name} y`).toBeLessThanOrEqual(1);
    }
  });

  it('flicks each ear far enough to be seen at the size the pet is drawn', () => {
    // The flick is a random, unprompted event, and filming one out of a breathing creature proved
    // unreliable: on a small ear the swing and the breath move the pixels by similar amounts. So
    // the motion is measured where it is decided - in the mesh - at the clip's peak. A flick that
    // moves the ear by less than a few pixels in a 260-pixel window is a flick nobody sees.
    const WINDOW_PX = 260;
    const MIN_VISIBLE_PX = 3;
    const skeleton = buildSkeleton(rig.bones);
    const mesh = buildGrid([0, 0, 1, 1], 22, 22, skeleton);
    const rest = new Float32Array(mesh.rest);
    for (const [clipName, boneName] of [
      ['ear_flick', 'ear.l'],
      ['ear_flick_r', 'ear.r'],
    ] as const) {
      const clip = rig.clips[clipName];
      if (!clip) continue; // ear_flick_r is optional; the scheduler falls back to ear_flick
      const track = clip.tracks.find((t) => t.bone === boneName && t.channel === 'rotate');
      expect(track, `${clipName} rotates ${boneName}`).toBeDefined();
      const peak = Math.max(...track!.keys.map((key) => Math.abs(key.value)));
      const pose = { [boneName]: { rotate: peak, x: 0, y: 0, scaleX: 1, scaleY: 1 } };
      const moved = skin(mesh, poseMatrices(skeleton, pose), new Float32Array(rest.length));
      let furthest = 0;
      for (let v = 0; v < rest.length; v += 2) {
        furthest = Math.max(furthest, Math.hypot(moved[v] - rest[v], moved[v + 1] - rest[v + 1]));
      }
      expect(furthest * WINDOW_PX, `${clipName} moves ${boneName}`).toBeGreaterThan(MIN_VISIBLE_PX);
    }
  });

  it('stands on its feet: the root is at the bottom and the head above it', () => {
    const pivot = (name: string) => rig.bones.find((bone) => bone.name === name)?.pivot;
    const root = pivot('root');
    const head = pivot('head');
    expect(root, 'root').toBeDefined();
    expect(head, 'head').toBeDefined();
    // y grows downwards, so a root above the head means the skeleton was measured upside down.
    expect(root![1]).toBeGreaterThan(0.8);
    expect(head![1]).toBeLessThan(root![1]);
  });
});
