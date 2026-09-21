/**
 * Bone hierarchy and the affine maths that turns a pose into one matrix per bone.
 *
 * The rule the whole rig rests on: a bone at rest is the identity. `restAngle` is not a rotation
 * applied to the picture — it is the *axis* the bone's own translation and scale act along, so
 * lengthening an ear that sits at 20° lengthens the ear rather than stretching the image sideways.
 * That is expressed by sandwiching the pose between the bone's rest rotation and its inverse:
 *
 *     local = T(pivot) · R(rest) · T(t) · R(rotate) · S(scale) · R(-rest) · T(-pivot)
 *     world = world(parent) · local
 *
 * Matrices are the flat `[a, b, c, d, tx, ty]` form Canvas2D uses, so a bone matrix can go straight
 * into `setTransform` in the 2D fallback.
 */

import type { Bone, BoneTransform, Matrix2D, Pose } from './types';
import { IDENTITY_TRANSFORM } from './types';

export const IDENTITY_MATRIX: Matrix2D = [1, 0, 0, 1, 0, 0];

const DEGREES_TO_RADIANS = Math.PI / 180;

/** `a` then `b` reading right to left: the result applies `b` first, exactly like `DOMMatrix`. */
export function multiply(a: Matrix2D, b: Matrix2D): Matrix2D {
  return [
    a[0] * b[0] + a[2] * b[1],
    a[1] * b[0] + a[3] * b[1],
    a[0] * b[2] + a[2] * b[3],
    a[1] * b[2] + a[3] * b[3],
    a[0] * b[4] + a[2] * b[5] + a[4],
    a[1] * b[4] + a[3] * b[5] + a[5],
  ];
}

export function applyMatrix(m: Matrix2D, x: number, y: number): [number, number] {
  return [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];
}

function rotation(degrees: number): Matrix2D {
  const radians = degrees * DEGREES_TO_RADIANS;
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  return [cos, sin, -sin, cos, 0, 0];
}

/** The matrix one bone contributes, in its parent's space. Identity for an unmoved bone. */
export function localMatrix(bone: Bone, transform: BoneTransform): Matrix2D {
  const [px, py] = bone.pivot;
  const pose: Matrix2D = multiply(
    [1, 0, 0, 1, transform.x, transform.y],
    multiply(rotation(transform.rotate), [transform.scaleX, 0, 0, transform.scaleY, 0, 0]),
  );
  const aligned = multiply(rotation(bone.restAngle), multiply(pose, rotation(-bone.restAngle)));
  return multiply([1, 0, 0, 1, px, py], multiply(aligned, [1, 0, 0, 1, -px, -py]));
}

/**
 * A skeleton: the bones in a parents-first order, with the index of each bone's parent resolved
 * once so every later frame is array lookups rather than map lookups.
 */
export interface Skeleton {
  readonly bones: readonly Bone[];
  readonly parentIndex: Int32Array;
  readonly indexOf: ReadonlyMap<string, number>;
}

export class SkeletonError extends Error {}

/**
 * Build a skeleton, checking the two things that silently produce nonsense otherwise: exactly one
 * root, and no bone referring to a parent that does not exist or has not been defined yet.
 */
export function buildSkeleton(bones: readonly Bone[]): Skeleton {
  if (bones.length === 0) throw new SkeletonError('a rig needs at least a root bone');
  const indexOf = new Map<string, number>();
  const parentIndex = new Int32Array(bones.length);
  let roots = 0;
  bones.forEach((bone, index) => {
    if (indexOf.has(bone.name)) throw new SkeletonError(`duplicate bone "${bone.name}"`);
    if (bone.parent === null) {
      roots += 1;
      parentIndex[index] = -1;
    } else {
      const parent = indexOf.get(bone.parent);
      if (parent === undefined) {
        throw new SkeletonError(
          `bone "${bone.name}" names parent "${bone.parent}", which is not defined above it`,
        );
      }
      parentIndex[index] = parent;
    }
    indexOf.set(bone.name, index);
  });
  if (roots !== 1) throw new SkeletonError(`a rig needs exactly one root bone, found ${roots}`);
  return { bones, parentIndex, indexOf };
}

/** World matrix per bone, in skeleton order. Bones absent from `pose` sit at rest. */
export function poseMatrices(skeleton: Skeleton, pose: Pose): Matrix2D[] {
  const world: Matrix2D[] = new Array(skeleton.bones.length);
  skeleton.bones.forEach((bone, index) => {
    const local = localMatrix(bone, pose[bone.name] ?? IDENTITY_TRANSFORM);
    const parent = skeleton.parentIndex[index];
    world[index] = parent < 0 ? local : multiply(world[parent], local);
  });
  return world;
}
