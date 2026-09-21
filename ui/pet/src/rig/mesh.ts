/**
 * The mesh: a triangulated grid over a texture, the weights that tie its vertices to bones, and
 * the skinning that moves them.
 *
 * Weighting is a normalised partition of unity: each bone declares how far its own influence
 * reaches, every vertex takes the bones that reach it in proportion, and a vertex no bone reaches
 * falls back to the root. Each bone therefore owns its own region and nothing else — an ear owns
 * the ear, the chest owns the ribcage.
 *
 * It is worth saying why the obvious alternative is wrong, because it was tried first: letting a
 * bone claim a share of whatever its *parent* still holds seems anatomically tidier, but it forces
 * every bone to reach across all of its descendants (an ear can only take weight the chest already
 * has), and once the chest reaches the ears it also reaches everything between them, so the last
 * bone in the chain ends up owning the whole creature. The hierarchy already does the job that
 * rule was reaching for: a head vertex weighted entirely to `head` still follows the chest,
 * because `head`'s world matrix is built through it.
 */

import { applyMatrix } from './bones';
import type { Skeleton } from './bones';
import type { Layer, Matrix2D } from './types';

export interface Mesh {
  /** Rest positions in normalised image coordinates, `[x0, y0, x1, y1, ...]`. */
  readonly rest: Float32Array;
  /** Texture coordinates into this layer's own texture, same order. */
  readonly uv: Float32Array;
  readonly indices: Uint16Array;
  /** Row-major `vertexCount x boneCount` weights; every row sums to 1. */
  readonly weights: Float32Array;
  readonly vertexCount: number;
}

/** Smoothstep falloff: 1 at the pivot, 0 at the radius, with zero slope at both ends. */
export function influenceAt(distance: number, radius: number, falloff: number): number {
  if (radius <= 0 || distance >= radius) return 0;
  const near = 1 - distance / radius;
  return Math.pow(near * near * (3 - 2 * near), falloff);
}

/** Below this total reach a vertex is treated as belonging to no bone, and goes to the root. */
const ORPHAN_INFLUENCE = 1e-4;

/**
 * Weights for one set of vertices. Returns a dense `vertexCount x boneCount` matrix: with a dozen
 * bones and a few hundred vertices that is a few kilobytes, and it keeps skinning a flat loop.
 *
 * Every row sums to exactly 1, which is what keeps the mesh from collapsing: a vertex that is
 * pulled by less than its whole weight shrinks towards the origin instead of standing still.
 */
export function computeWeights(skeleton: Skeleton, positions: Float32Array): Float32Array {
  const boneCount = skeleton.bones.length;
  const vertexCount = positions.length / 2;
  const weights = new Float32Array(vertexCount * boneCount);
  const rootIndex = skeleton.parentIndex.indexOf(-1);
  for (let v = 0; v < vertexCount; v += 1) {
    const row = v * boneCount;
    const x = positions[v * 2];
    const y = positions[v * 2 + 1];
    let total = 0;
    for (let b = 0; b < boneCount; b += 1) {
      const bone = skeleton.bones[b];
      if (bone.channelOnly) continue;
      const reach = influenceAt(
        Math.hypot(x - bone.pivot[0], y - bone.pivot[1]),
        bone.influence.radius,
        bone.influence.falloff,
      );
      weights[row + b] = reach;
      total += reach;
    }
    if (total < ORPHAN_INFLUENCE) {
      weights.fill(0, row, row + boneCount);
      weights[row + rootIndex] = 1;
      continue;
    }
    for (let b = 0; b < boneCount; b += 1) weights[row + b] /= total;
  }
  return weights;
}

/** A regular grid over `rect`, triangulated, with UVs spanning the layer's own texture. */
export function buildGrid(
  rect: readonly [number, number, number, number],
  columns: number,
  rows: number,
  skeleton: Skeleton,
): Mesh {
  const [left, top, right, bottom] = rect;
  const vertexCount = (columns + 1) * (rows + 1);
  const rest = new Float32Array(vertexCount * 2);
  const uv = new Float32Array(vertexCount * 2);
  for (let row = 0; row <= rows; row += 1) {
    for (let column = 0; column <= columns; column += 1) {
      const index = row * (columns + 1) + column;
      const u = column / columns;
      const v = row / rows;
      rest[index * 2] = left + (right - left) * u;
      rest[index * 2 + 1] = top + (bottom - top) * v;
      uv[index * 2] = u;
      uv[index * 2 + 1] = v;
    }
  }
  const indices = new Uint16Array(columns * rows * 6);
  let cursor = 0;
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const topLeft = row * (columns + 1) + column;
      const topRight = topLeft + 1;
      const bottomLeft = topLeft + columns + 1;
      const bottomRight = bottomLeft + 1;
      indices.set([topLeft, topRight, bottomLeft, topRight, bottomRight, bottomLeft], cursor);
      cursor += 6;
    }
  }
  return { rest, uv, indices, weights: computeWeights(skeleton, rest), vertexCount };
}

export function buildLayerMesh(layer: Layer, skeleton: Skeleton): Mesh {
  return buildGrid(layer.rect, layer.grid.columns, layer.grid.rows, skeleton);
}

/**
 * Linear blend skinning: move every rest vertex by the weighted average of its bones' matrices.
 *
 * Writes into `out` rather than allocating, because this runs once per layer per frame and a fresh
 * Float32Array 180 times a second is the difference between a quiet pet and a busy one.
 */
export function skin(mesh: Mesh, matrices: readonly Matrix2D[], out: Float32Array): Float32Array {
  const boneCount = matrices.length;
  for (let v = 0; v < mesh.vertexCount; v += 1) {
    const row = v * boneCount;
    const x = mesh.rest[v * 2];
    const y = mesh.rest[v * 2 + 1];
    let sumX = 0;
    let sumY = 0;
    for (let b = 0; b < boneCount; b += 1) {
      const weight = mesh.weights[row + b];
      if (weight === 0) continue;
      const [bx, by] = applyMatrix(matrices[b], x, y);
      sumX += bx * weight;
      sumY += by * weight;
    }
    out[v * 2] = sumX;
    out[v * 2 + 1] = sumY;
  }
  return out;
}

/**
 * Close a layer onto a horizontal line, in place: the eyelid.
 *
 * Applied after skinning so the eye first follows the head and only then shuts. `amount` is 0 for
 * wide open and 1 for fully closed, and the line is the lower lid, so the *top* of the eye travels
 * down over it — which is the way a lid actually closes.
 *
 * `pivotY` is in the same space as `positions`, which means the caller has already put the rest
 * lid line through the head's matrix: a lid on a tilted head has to tilt with it.
 */
export function closeLid(positions: Float32Array, pivotY: number, amount: number): void {
  const openness = 1 - Math.min(1, Math.max(0, amount));
  for (let i = 1; i < positions.length; i += 2) {
    positions[i] = pivotY + (positions[i] - pivotY) * openness;
  }
}
