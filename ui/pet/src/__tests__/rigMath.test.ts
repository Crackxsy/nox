/**
 * The rig's arithmetic: bone transforms, the skeleton's invariants, vertex weighting and skinning.
 *
 * These are the parts that are invisible when they are wrong — a rig with a mis-parented bone or a
 * weight matrix whose rows do not sum to one still renders, it just renders a creature that sags.
 * Every test here names the property that keeps the picture honest.
 */

import { describe, expect, it } from 'vitest';

import { applyMatrix, buildSkeleton, localMatrix, multiply, poseMatrices, SkeletonError } from '../rig/bones';
import { buildGrid, closeLid, computeWeights, influenceAt, skin } from '../rig/mesh';
import type { Bone, BoneTransform, Matrix2D } from '../rig/types';
import { IDENTITY_TRANSFORM } from '../rig/types';

function bone(name: string, parent: string | null, pivot: [number, number], extra: Partial<Bone> = {}): Bone {
  return {
    name,
    parent,
    pivot,
    restAngle: 0,
    influence: { radius: 0.3, falloff: 1 },
    channelOnly: false,
    ...extra,
  };
}

const CHAIN: Bone[] = [
  bone('root', null, [0.5, 1], { influence: { radius: 0.4, falloff: 1 } }),
  bone('body', 'root', [0.5, 0.7]),
  bone('head', 'body', [0.5, 0.3]),
];

function transform(partial: Partial<BoneTransform>): BoneTransform {
  return { ...IDENTITY_TRANSFORM, ...partial };
}

function around(value: number, expected: number, tolerance = 1e-6): void {
  expect(Math.abs(value - expected)).toBeLessThan(tolerance);
}

describe('bone transforms', () => {
  it('leaves every point untouched when the bone is at rest', () => {
    const matrix = localMatrix(bone('head', 'body', [0.3, 0.42]), IDENTITY_TRANSFORM);
    const [x, y] = applyMatrix(matrix, 0.9, 0.1);
    around(x, 0.9);
    around(y, 0.1);
  });

  it('rotates about its own pivot and not about the image origin', () => {
    const head = bone('head', null, [0.5, 0.5]);
    const matrix = localMatrix(head, transform({ rotate: 90 }));
    const [pivotX, pivotY] = applyMatrix(matrix, 0.5, 0.5);
    around(pivotX, 0.5);
    around(pivotY, 0.5);
    const [x, y] = applyMatrix(matrix, 0.6, 0.5);
    around(x, 0.5);
    around(y, 0.6);
  });

  it('applies scale along the bone rest axis, so a slanted ear lengthens along the ear', () => {
    // A bone resting at -90 degrees points up the image; scaling its x axis must move the point
    // that sits above the pivot, not the one beside it.
    const ear = bone('ear', null, [0.5, 0.5], { restAngle: -90 });
    const matrix = localMatrix(ear, transform({ scaleX: 2 }));
    const [aboveX, aboveY] = applyMatrix(matrix, 0.5, 0.4);
    around(aboveX, 0.5);
    around(aboveY, 0.3);
    const [besideX, besideY] = applyMatrix(matrix, 0.6, 0.5);
    around(besideX, 0.6);
    around(besideY, 0.5);
  });

  it('composes parent before child, so a child inherits its parent travel', () => {
    const skeleton = buildSkeleton(CHAIN);
    const matrices = poseMatrices(skeleton, { body: transform({ y: -0.1 }) });
    const [, headY] = applyMatrix(matrices[2], 0.5, 0.3);
    around(headY, 0.2);
  });

  it('multiplies matrices in apply-second-first order', () => {
    const translate: Matrix2D = [1, 0, 0, 1, 0.2, 0];
    const scale: Matrix2D = [2, 0, 0, 2, 0, 0];
    const [x] = applyMatrix(multiply(translate, scale), 1, 0);
    around(x, 2.2);
  });
});

describe('skeleton validation', () => {
  it('rejects a rig with no root', () => {
    expect(() => buildSkeleton([bone('a', 'b', [0, 0]), bone('b', 'a', [0, 0])])).toThrow(SkeletonError);
  });

  it('rejects two roots, because the second one would silently never move the first', () => {
    expect(() => buildSkeleton([bone('a', null, [0, 0]), bone('b', null, [0, 0])])).toThrow(
      /exactly one root/,
    );
  });

  it('rejects a parent defined after its child, which would break the transform order', () => {
    expect(() => buildSkeleton([bone('root', null, [0, 0]), bone('a', 'b', [0, 0]), bone('b', 'root', [0, 0])])).toThrow(
      /not defined above it/,
    );
  });

  it('rejects duplicate bone names', () => {
    expect(() => buildSkeleton([bone('root', null, [0, 0]), bone('root', 'root', [0, 0])])).toThrow(
      /duplicate bone/,
    );
  });
});

describe('vertex weighting', () => {
  const skeleton = buildSkeleton(CHAIN);

  it('falls off smoothly from the pivot to the radius and is zero beyond it', () => {
    around(influenceAt(0, 0.2, 1), 1);
    expect(influenceAt(0.1, 0.2, 1)).toBeGreaterThan(0);
    expect(influenceAt(0.1, 0.2, 1)).toBeLessThan(1);
    expect(influenceAt(0.2, 0.2, 1)).toBe(0);
    expect(influenceAt(0.9, 0.2, 1)).toBe(0);
  });

  it('gives every vertex weights that sum to one, so nothing collapses towards the origin', () => {
    const positions = new Float32Array([0.5, 0.3, 0.5, 0.7, 0.5, 1, 0.05, 0.05, 0.99, 0.02]);
    const weights = computeWeights(skeleton, positions);
    for (let vertex = 0; vertex < positions.length / 2; vertex += 1) {
      let sum = 0;
      for (let b = 0; b < CHAIN.length; b += 1) sum += weights[vertex * CHAIN.length + b];
      around(sum, 1, 1e-5);
    }
  });

  it('gives a vertex to the bone whose pivot it sits on', () => {
    const weights = computeWeights(skeleton, new Float32Array([0.5, 0.3]));
    expect(weights[2]).toBeGreaterThan(0.9);
  });

  it('hands a vertex no bone reaches to the root rather than leaving it weightless', () => {
    const weights = computeWeights(skeleton, new Float32Array([0.02, 0.02]));
    around(weights[0], 1);
  });

  it('ignores channel-only bones, which drive a layer and must not also deform the skin', () => {
    const withLid = buildSkeleton([
      ...CHAIN,
      bone('lid', 'head', [0.5, 0.3], { channelOnly: true, influence: { radius: 0.3, falloff: 1 } }),
    ]);
    const weights = computeWeights(withLid, new Float32Array([0.5, 0.3]));
    expect(weights[3]).toBe(0);
    expect(weights[2]).toBeGreaterThan(0.9);
  });
});

describe('mesh and skinning', () => {
  const skeleton = buildSkeleton(CHAIN);

  it('builds a grid whose corners are the rect and whose UVs span the texture', () => {
    const mesh = buildGrid([0.2, 0.4, 0.6, 1], 2, 2, skeleton);
    expect(mesh.vertexCount).toBe(9);
    expect(mesh.indices.length).toBe(2 * 2 * 6);
    around(mesh.rest[0], 0.2);
    around(mesh.rest[1], 0.4);
    around(mesh.rest[mesh.rest.length - 2], 0.6);
    around(mesh.rest[mesh.rest.length - 1], 1);
    around(mesh.uv[0], 0);
    around(mesh.uv[mesh.uv.length - 1], 1);
  });

  it('leaves the mesh exactly where it was when no bone has moved', () => {
    const mesh = buildGrid([0, 0, 1, 1], 4, 4, skeleton);
    const out = new Float32Array(mesh.vertexCount * 2);
    skin(mesh, poseMatrices(skeleton, {}), out);
    for (let i = 0; i < out.length; i += 1) around(out[i], mesh.rest[i], 1e-5);
  });

  it('moves a vertex by its bone share, not by the whole bone travel', () => {
    const mesh = buildGrid([0.5, 0.3, 0.51, 0.31], 2, 2, skeleton);
    const out = new Float32Array(mesh.vertexCount * 2);
    skin(mesh, poseMatrices(skeleton, { head: transform({ y: -0.1 }) }), out);
    const travel = mesh.rest[1] - out[1];
    expect(travel).toBeGreaterThan(0.09);
    expect(travel).toBeLessThanOrEqual(0.1);
  });

  it('closes a lid onto its line, leaving that line where it was', () => {
    const positions = new Float32Array([0, 0.2, 0, 0.3, 0, 0.4]);
    closeLid(positions, 0.4, 1);
    for (let i = 1; i < positions.length; i += 2) around(positions[i], 0.4, 1e-6);
  });

  it('closes a lid halfway to exactly half the distance', () => {
    const positions = new Float32Array([0, 0.2]);
    closeLid(positions, 0.4, 0.5);
    around(positions[1], 0.3);
  });

  it('leaves a lid alone at zero and clamps beyond one', () => {
    const open = new Float32Array([0, 0.2]);
    closeLid(open, 0.4, 0);
    around(open[1], 0.2);
    const overshoot = new Float32Array([0, 0.2]);
    closeLid(overshoot, 0.4, 3);
    around(overshoot[1], 0.4);
  });
});
