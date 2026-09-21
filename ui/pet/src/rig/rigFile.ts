/**
 * Reading and validating `variants/<id>/rig.json`.
 *
 * `rig.json` is hand-edited — the pivots in it were placed by eye against the photograph and will
 * be nudged again — so every failure here has to name the field and say what was wrong with it.
 * The caller's answer to any error is the same and is the point of the whole file: fall back to
 * the plain sprite path, which still works, and log the reason.
 *
 * A variant with no `rig.json` at all is not an error. It is the normal case for every variant
 * that has not been rigged yet, and `loadRig` reports it as `null` rather than throwing.
 */

import { isEasingName } from './clips';
import type {
  Bone,
  Channel,
  Clip,
  EasingName,
  Keyframe,
  Layer,
  PoseFrame,
  RigDefinition,
  Track,
} from './types';

export class RigDefinitionError extends Error {}

/** Same rule as the sprite manifest: nothing in a rig file may walk out of its own directory. */
const SAFE_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const CHANNELS: readonly Channel[] = ['rotate', 'x', 'y', 'scaleX', 'scaleY'];

/** Grid limits. Below 2 there is nothing to deform; above 64 the mesh costs more than it buys. */
const MIN_GRID = 2;
const MAX_GRID = 64;
const MAX_CLIP_MS = 120_000;

function fail(where: string, what: string): never {
  throw new RigDefinitionError(`${where}: ${what}`);
}

function asObject(value: unknown, where: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    fail(where, 'must be a JSON object');
  }
  return value as Record<string, unknown>;
}

function asNumber(value: unknown, where: string, min: number, max: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) fail(where, 'must be a number');
  if (value < min || value > max) fail(where, `must be between ${min} and ${max}, got ${value}`);
  return value;
}

function asSegment(value: unknown, where: string): string {
  if (typeof value !== 'string' || !SAFE_SEGMENT.test(value) || value.includes('..')) {
    fail(where, 'must be a plain file or directory name');
  }
  return value;
}

function asName(value: unknown, where: string): string {
  // Bone names carry dots (`ear.l`), so they are looser than file names but still not paths.
  if (typeof value !== 'string' || !/^[a-z][a-z0-9._-]*$/.test(value)) {
    fail(where, 'must be a lower-case name like "ear.l"');
  }
  return value;
}

function asNormalisedPoint(value: unknown, where: string): [number, number] {
  if (!Array.isArray(value) || value.length !== 2) fail(where, 'must be [x, y]');
  return [asNumber(value[0], `${where}[0]`, 0, 1), asNumber(value[1], `${where}[1]`, 0, 1)];
}

function parseBone(raw: unknown, where: string): Bone {
  const bone = asObject(raw, where);
  const influence = asObject(bone.influence ?? {}, `${where}.influence`);
  return {
    name: asName(bone.name, `${where}.name`),
    parent: bone.parent === null || bone.parent === undefined ? null : asName(bone.parent, `${where}.parent`),
    pivot: asNormalisedPoint(bone.pivot, `${where}.pivot`),
    restAngle: asNumber(bone.restAngle ?? 0, `${where}.restAngle`, -360, 360),
    influence: {
      radius: asNumber(influence.radius ?? 0.2, `${where}.influence.radius`, 0.001, 4),
      falloff: asNumber(influence.falloff ?? 1, `${where}.influence.falloff`, 0.05, 8),
    },
    channelOnly: bone.channelOnly === true,
  };
}

function parseRect(raw: unknown, where: string): [number, number, number, number] {
  if (!Array.isArray(raw) || raw.length !== 4) fail(where, 'must be [left, top, right, bottom]');
  const rect: [number, number, number, number] = [
    asNumber(raw[0], `${where}[0]`, 0, 1),
    asNumber(raw[1], `${where}[1]`, 0, 1),
    asNumber(raw[2], `${where}[2]`, 0, 1),
    asNumber(raw[3], `${where}[3]`, 0, 1),
  ];
  if (rect[2] <= rect[0] || rect[3] <= rect[1]) fail(where, 'right/bottom must exceed left/top');
  return rect;
}

function parseLayer(raw: unknown, where: string, boneNames: ReadonlySet<string>): Layer {
  const layer = asObject(raw, where);
  const grid = asObject(layer.grid ?? {}, `${where}.grid`);
  const lidRaw = layer.lid;
  let lid: Layer['lid'];
  if (lidRaw !== undefined) {
    const parsed = asObject(lidRaw, `${where}.lid`);
    const bone = asName(parsed.bone, `${where}.lid.bone`);
    if (!boneNames.has(bone)) fail(`${where}.lid.bone`, `no bone named "${bone}"`);
    lid = { bone };
  }
  return {
    name: asName(layer.name, `${where}.name`),
    file: asSegment(layer.file, `${where}.file`),
    rect: parseRect(layer.rect, `${where}.rect`),
    grid: {
      columns: asNumber(grid.columns ?? 4, `${where}.grid.columns`, MIN_GRID, MAX_GRID),
      rows: asNumber(grid.rows ?? 4, `${where}.grid.rows`, MIN_GRID, MAX_GRID),
    },
    lid,
  };
}

function parseKeys(raw: unknown, where: string): Keyframe[] {
  if (!Array.isArray(raw) || raw.length < 2) fail(where, 'needs at least two keyframes');
  const keys = raw.map((entry, index) => {
    const key = asObject(entry, `${where}[${index}]`);
    const easing = key.easing;
    if (easing !== undefined && (typeof easing !== 'string' || !isEasingName(easing))) {
      fail(`${where}[${index}].easing`, `unknown easing "${String(easing)}"`);
    }
    return {
      t: asNumber(key.t, `${where}[${index}].t`, 0, 1),
      value: asNumber(key.value, `${where}[${index}].value`, -1000, 1000),
      easing: easing as EasingName | undefined,
    };
  });
  if (keys[0].t !== 0) fail(where, 'the first keyframe must be at t = 0');
  for (let i = 1; i < keys.length; i += 1) {
    if (keys[i].t <= keys[i - 1].t) fail(`${where}[${i}].t`, 'keyframes must be strictly ascending');
  }
  return keys;
}

function parseTrack(raw: unknown, where: string, boneNames: ReadonlySet<string>): Track {
  const track = asObject(raw, where);
  const bone = asName(track.bone, `${where}.bone`);
  if (!boneNames.has(bone)) fail(`${where}.bone`, `no bone named "${bone}"`);
  const channel = track.channel;
  if (typeof channel !== 'string' || !(CHANNELS as readonly string[]).includes(channel)) {
    fail(`${where}.channel`, `must be one of ${CHANNELS.join(', ')}`);
  }
  return { bone, channel: channel as Channel, keys: parseKeys(track.keys, `${where}.keys`) };
}

function parseClip(raw: unknown, name: string, boneNames: ReadonlySet<string>): Clip {
  const where = `clips.${name}`;
  const clip = asObject(raw, where);
  const easing = clip.easing ?? 'sine';
  if (typeof easing !== 'string' || !isEasingName(easing)) {
    fail(`${where}.easing`, `unknown easing "${String(easing)}"`);
  }
  const tracks = clip.tracks;
  if (!Array.isArray(tracks) || tracks.length === 0) fail(`${where}.tracks`, 'must be a non-empty array');
  return {
    name,
    durationMs: asNumber(clip.durationMs, `${where}.durationMs`, 1, MAX_CLIP_MS),
    loop: clip.loop === true,
    easing,
    tracks: tracks.map((track, index) => parseTrack(track, `${where}.tracks[${index}]`, boneNames)),
  };
}

/**
 * Validate an untrusted `rig.json`. Throws `RigDefinitionError` naming the field that was wrong.
 *
 * Cross-checks that a per-field schema cannot see: exactly one root bone, parents defined before
 * their children, every track and lid pointing at a bone that exists, and the base texture being a
 * real file name rather than a path.
 */
export function parseRigDefinition(raw: unknown, expectedId: string): RigDefinition {
  const root = asObject(raw, 'rig.json');
  const id = asSegment(root.id, 'id');
  if (id !== expectedId) fail('id', `"${id}" does not match the directory "${expectedId}"`);

  const bonesRaw = root.bones;
  if (!Array.isArray(bonesRaw) || bonesRaw.length === 0) fail('bones', 'must be a non-empty array');
  const bones = bonesRaw.map((bone, index) => parseBone(bone, `bones[${index}]`));
  const boneNames = new Set(bones.map((bone) => bone.name));
  if (boneNames.size !== bones.length) fail('bones', 'bone names must be unique');
  const seen = new Set<string>();
  let roots = 0;
  for (const bone of bones) {
    if (bone.parent === null) roots += 1;
    else if (!seen.has(bone.parent)) fail(`bones.${bone.name}.parent`, `"${bone.parent}" is not defined above it`);
    seen.add(bone.name);
  }
  if (roots !== 1) fail('bones', `exactly one bone must have parent null, found ${roots}`);

  const layersRaw = root.layers ?? [];
  if (!Array.isArray(layersRaw)) fail('layers', 'must be an array');
  const layers = layersRaw.map((layer, index) => parseLayer(layer, `layers[${index}]`, boneNames));

  const clipsRaw = asObject(root.clips ?? {}, 'clips');
  const clips: Record<string, Clip> = {};
  for (const [name, clip] of Object.entries(clipsRaw)) {
    clips[asName(name, `clips."${name}"`)] = parseClip(clip, name, boneNames);
  }

  const posesRaw = asObject(root.poses ?? {}, 'poses');
  const poses: Record<string, PoseFrame> = {};
  for (const [name, pose] of Object.entries(posesRaw)) {
    const where = `poses.${name}`;
    poses[asName(name, where)] = { name, file: asSegment(asObject(pose, where).file, `${where}.file`) };
  }

  const mesh = asObject(root.mesh ?? {}, 'mesh');
  return {
    id,
    base: asSegment(root.base, 'base'),
    assetDir: asSegment(root.assetDir ?? 'rig', 'assetDir'),
    bones,
    layers,
    clips,
    poses,
    mesh: {
      columns: asNumber(mesh.columns ?? 20, 'mesh.columns', MIN_GRID, MAX_GRID),
      rows: asNumber(mesh.rows ?? 20, 'mesh.rows', MIN_GRID, MAX_GRID),
    },
  };
}

/** Every image file a rig needs, in a stable order, as directory-relative names. */
export function rigFiles(rig: RigDefinition): string[] {
  const files = [rig.base, ...rig.layers.map((layer) => layer.file)];
  for (const pose of Object.values(rig.poses)) files.push(pose.file);
  return [...new Set(files)];
}

export function rigUrl(variantBase: string, rig: RigDefinition, file: string): string {
  return `${variantBase}${rig.assetDir}/${file}`;
}

export interface LoadedRig {
  definition: RigDefinition;
  /** Decoded image per file name. Poses whose file did not decode are absent and unavailable. */
  images: Record<string, TexImageSource>;
  /** Pose names named in the rig whose art does not exist yet. Reported, never faked. */
  missingPoses: string[];
}

export interface RigLoadOptions {
  fetchJson: (url: string) => Promise<unknown>;
  loadImage: (url: string) => Promise<TexImageSource>;
  /** Directory the variant is served from, ending in a slash. */
  variantBase: string;
}

/**
 * Fetch, validate and preload a rig. Resolves to `null` when the variant simply has no `rig.json`
 * — that is the unrigged-but-fine case — and throws `RigDefinitionError` when there is one and it
 * cannot be used, which is the caller's signal to stay on the sprite path.
 *
 * A *pose* frame that fails to load does not fail the rig: the named key poses are art that does
 * not exist yet, so they are collected into `missingPoses` and the pet keeps its base drawing.
 */
export async function loadRig(
  expectedId: string,
  options: RigLoadOptions,
): Promise<LoadedRig | null> {
  let raw: unknown;
  try {
    raw = await options.fetchJson(`${options.variantBase}rig.json`);
  } catch {
    return null;
  }
  if (raw === null) return null;
  const definition = parseRigDefinition(raw, expectedId);

  const required = [definition.base, ...definition.layers.map((layer) => layer.file)];
  const optional = Object.entries(definition.poses).map(([name, pose]) => ({ name, file: pose.file }));
  const images: Record<string, TexImageSource> = {};

  const requiredResults = await Promise.allSettled(
    required.map((file) => options.loadImage(rigUrl(options.variantBase, definition, file))),
  );
  const brokenFiles = required.filter((_, index) => requiredResults[index].status === 'rejected');
  if (brokenFiles.length > 0) {
    throw new RigDefinitionError(`texture(s) failed to load: ${brokenFiles.join(', ')}`);
  }
  required.forEach((file, index) => {
    const result = requiredResults[index];
    if (result.status === 'fulfilled') images[file] = result.value;
  });

  const missingPoses: string[] = [];
  const poseResults = await Promise.allSettled(
    optional.map((pose) => options.loadImage(rigUrl(options.variantBase, definition, pose.file))),
  );
  optional.forEach((pose, index) => {
    const result = poseResults[index];
    if (result.status === 'fulfilled') images[pose.file] = result.value;
    else missingPoses.push(pose.name);
  });

  return { definition, images, missingPoses };
}
