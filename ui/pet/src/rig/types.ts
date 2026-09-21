/**
 * The vocabulary of the 2D deformation rig: bones, poses, clips and layers.
 *
 * Everything here is data. It is what `rig.json` parses into (`rigFile.ts`), what the maths
 * modules consume, and what the renderer draws. Nothing in this file touches the DOM, WebGL or
 * React, so the whole rig can be reasoned about — and tested — as pure functions over these types.
 *
 * Coordinates are *normalised image coordinates* throughout: (0, 0) is the top-left of the base
 * texture, (1, 1) its bottom-right, independent of how large the texture or the window happens to
 * be. A rig authored against a 1024 px photo therefore keeps working against a 2048 px re-render.
 */

/** An affine 2x3 matrix `[a, b, c, d, tx, ty]`: `x' = a*x + c*y + tx`, `y' = b*x + d*y + ty`. */
export type Matrix2D = readonly [number, number, number, number, number, number];

/**
 * One bone's departure from its rest pose. Identity is "the bone has not moved".
 *
 * `rotate` is degrees about the bone's own pivot. `x`/`y` are normalised image units along the
 * bone's rest axes, so translating an ear that rests at 20° moves it along the ear, not along the
 * picture. `scaleX`/`scaleY` are multipliers on those same axes.
 */
export interface BoneTransform {
  rotate: number;
  x: number;
  y: number;
  scaleX: number;
  scaleY: number;
}

export const IDENTITY_TRANSFORM: BoneTransform = { rotate: 0, x: 0, y: 0, scaleX: 1, scaleY: 1 };

/** A complete pose: every bone that has moved, by name. Bones left out are at rest. */
export type Pose = Readonly<Record<string, BoneTransform>>;

export interface BoneInfluence {
  /** Reach of the bone in normalised image units; beyond it the bone has no say at all. */
  radius: number;
  /**
   * Shape of the falloff inside the radius. 1 is a plain smoothstep; above 1 the influence hugs
   * the pivot (a tight ear), below 1 it spreads (a whole flank).
   */
  falloff: number;
}

export interface Bone {
  name: string;
  /** `null` only for the single root. Every other parent must exist and come earlier. */
  parent: string | null;
  pivot: readonly [number, number];
  /** Degrees, clockwise from +x. Defines the axes `x`/`y`/`scaleX`/`scaleY` act along. */
  restAngle: number;
  influence: BoneInfluence;
  /**
   * The bone carries animation but moves no vertices. Used for an eyelid, whose closing is a
   * layer operation (`closeLid`) rather than a deformation of the skin: without this the lid would
   * be applied twice, once through the mesh and once through the layer.
   */
  channelOnly: boolean;
}

export type EasingName = 'linear' | 'sine' | 'easeIn' | 'easeOut' | 'easeInOut' | 'step';

export type Channel = keyof BoneTransform;

export interface Keyframe {
  /** Position within the clip, 0..1. Keys are sorted and the first must be 0. */
  t: number;
  value: number;
  /** Easing from this key to the next. Defaults to the track's, then the clip's. */
  easing?: EasingName;
}

export interface Track {
  bone: string;
  channel: Channel;
  keys: readonly Keyframe[];
}

export interface Clip {
  name: string;
  durationMs: number;
  loop: boolean;
  easing: EasingName;
  tracks: readonly Track[];
}

/**
 * A texture drawn by the rig. The base layer covers the whole image; the eye layers are small
 * cut-outs pinned to a rectangle of it, which is what lets a blink be a lid closing over an
 * eyeball instead of a dip in brightness.
 */
export interface Layer {
  name: string;
  file: string;
  /** Where this texture sits in normalised image coordinates: `[left, top, right, bottom]`. */
  rect: readonly [number, number, number, number];
  /** Mesh resolution for this layer. The base wants a grid; a 40 px eye wants almost none. */
  grid: { columns: number; rows: number };
  /**
   * Bone whose `scaleY` closes this layer like an eyelid: 1 is wide open, 0 fully shut. The lid
   * closes onto that bone's own pivot, so the line it shuts along travels with the head. Only the
   * eye layers set it; everything else deforms through the skin like the base texture.
   */
  lid?: { bone: string };
}

/** An alternative whole-body drawing the player can cross-dissolve to while the mesh keeps running. */
export interface PoseFrame {
  name: string;
  file: string;
}

export interface RigDefinition {
  /** Must equal the variant directory name, exactly as `sprites.json` must. */
  id: string;
  /** Texture the mesh is built over; all `rect`s are relative to it. */
  base: string;
  /** Directory the files above live in, relative to the variant directory. */
  assetDir: string;
  bones: readonly Bone[];
  layers: readonly Layer[];
  clips: Readonly<Record<string, Clip>>;
  poses: Readonly<Record<string, PoseFrame>>;
  mesh: { columns: number; rows: number };
}
