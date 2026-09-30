/**
 * Key poses: what gets drawn, and with which geometry.
 *
 * A key pose is a *different picture*, not a deformation of the base one, and the two rules that
 * follow from that are the whole of this file. The mesh's bone weights are painted against the base
 * photograph, so a patch of texture is "the head" only in that picture; skinning a curled-up body
 * with them turns the head bone loose on whatever sits at those coordinates. And the eyes are
 * separate layers placed by the skeleton, so drawing them over a curled body left two eyes hanging
 * in the air.
 *
 * Both were visible only by looking at the running pet, which is exactly why they are asserted here.
 */

import { describe, expect, it } from 'vitest';

import type { DrawCall, RigRenderer } from '../rig/renderer';
import { RigScene } from '../rig/scene';
import type { LoadedRig } from '../rig/rigFile';

const TEXTURE = { width: 4, height: 4 } as unknown as TexImageSource;

const DEFINITION = {
  id: 'test',
  base: 'base.png',
  assetDir: 'rig',
  mesh: { columns: 4, rows: 4 },
  bones: [
    { name: 'root', parent: null, pivot: [0.5, 0.9], restAngle: 0, influence: { radius: 1.5, falloff: 1 } },
    { name: 'head', parent: 'root', pivot: [0.5, 0.2], restAngle: 0, influence: { radius: 0.6, falloff: 1 } },
  ],
  layers: [
    { name: 'eye', file: 'eye.png', rect: [0.4, 0.2, 0.6, 0.3], grid: { columns: 2, rows: 2 } },
  ],
  poses: { curl: { file: 'pose_curl.png' } },
  clips: {},
} as unknown as LoadedRig['definition'];

function loaded(): LoadedRig {
  return {
    definition: DEFINITION,
    images: {
      'base.png': TEXTURE,
      'eye.png': TEXTURE,
      'pose_curl.png': { width: 8, height: 8 } as unknown as TexImageSource,
    },
    missingPoses: [],
  } as unknown as LoadedRig;
}

class Recorder implements RigRenderer {
  readonly backend = 'canvas2d' as const;
  readonly downgradeReason = null;
  readonly triangleBudget = 4096;
  frames: DrawCall[][] = [];

  draw(calls: readonly DrawCall[]): void {
    this.frames.push(calls.map((call) => ({ ...call })));
  }

  resize(): void {}
  dispose(): void {}
}

function sceneWith(): { scene: RigScene; recorder: Recorder } {
  const recorder = new Recorder();
  return { scene: new RigScene(loaded(), recorder), recorder };
}

const last = (recorder: Recorder): DrawCall[] => recorder.frames[recorder.frames.length - 1];

describe('key poses', () => {
  it('draws the base picture skinned, with its eyes', () => {
    const { scene, recorder } = sceneWith();

    scene.frame(0, 0);

    const calls = last(recorder);
    expect(calls).toHaveLength(2);
    expect(calls[1].alpha).toBe(1);
  });

  it('draws a pose from its own geometry, not through the skin buffer', () => {
    const { scene, recorder } = sceneWith();
    scene.setPose('curl', 0);

    scene.frame(100, 0); // mid-dissolve: the base picture and the pose are both on screen

    const [base, pose] = last(recorder);
    // The base picture is skinned into one shared buffer. The pose must not ride on it - the bone
    // weights describe the base photograph and mean nothing for a differently shaped drawing.
    expect(pose.positions).not.toBe(base.positions);
  });

  it('takes the eyes away with the base picture', () => {
    const { scene, recorder } = sceneWith();
    scene.setPose('curl', 0);

    scene.frame(10_000, 0);

    // One call: the pose. The eye layer is gone, because the pose carries its own face.
    expect(last(recorder)).toHaveLength(1);
  });

  it('fades the eyes out over the dissolve rather than cutting them', () => {
    const { scene, recorder } = sceneWith();
    scene.setPose('curl', 0);

    scene.frame(100, 0); // mid-dissolve

    const calls = last(recorder);
    const eye = calls[calls.length - 1];
    expect(eye.alpha).toBeGreaterThan(0);
    expect(eye.alpha).toBeLessThan(1);
    // Exactly as visible as the base drawing it belongs to.
    expect(eye.alpha).toBeCloseTo(calls[0].alpha, 5);
  });

  it('brings the eyes back when the pose is released', () => {
    const { scene, recorder } = sceneWith();
    scene.setPose('curl', 0);
    scene.frame(10_000, 0);

    scene.setPose(null, 10_000);
    scene.frame(20_000, 0);

    expect(last(recorder)).toHaveLength(2);
  });

  it('refuses a pose it has no art for, so the caller can fall back', () => {
    const { scene } = sceneWith();

    expect(scene.setPose('sit', 0)).toBe(false);
    expect(scene.setPose('curl', 0)).toBe(true);
  });
});
