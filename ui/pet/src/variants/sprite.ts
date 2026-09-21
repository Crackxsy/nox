/**
 * Sprite variant (#19): the renderer side of the creature the product owner will actually pick.
 *
 * The procedural variants in this folder draw the pet from parameters. A *sprite* variant instead
 * plays pre-rendered frames — one PNG-with-alpha sequence per expression — and is selected with
 * `pet.variant: sprite:<id>` (plain ids keep meaning "procedural"). The art is not drawn yet; what
 * exists here is the whole path around it, plus one generated placeholder set under
 * `public/variants/placeholder/` so that path can be exercised end to end.
 *
 * Nothing in this module touches the DOM: it is the pure half (manifest parsing, state -> frame
 * mapping, fallback decisions) so it can be unit-tested in vitest's `node` environment. The React
 * half lives in `../SpritePet.tsx`.
 *
 * Deliberately *not* a second state machine: `petState.ts` stays the only place that decides what
 * Nox is doing. `spriteExpressionFor` is a projection of that state onto the handful of expressions
 * an artist can reasonably be asked to draw, and it degrades by falling back, never by inventing.
 */

import type { PetInput } from '../petState';

/** Expressions a sprite set may provide. `idle` is the only mandatory one; `blink` is an optional
 * overlay sequence played briefly on top of a resting expression, not a state the pet machine can
 * itself be in. */
export const SPRITE_EXPRESSIONS = [
  'idle',
  'listening',
  'speaking',
  'sleeping',
  'thinking',
  'blink',
] as const;
export type SpriteExpression = (typeof SPRITE_EXPRESSIONS)[number];

/** The expressions `spriteExpressionFor` can return (everything but the blink overlay). */
export type SpriteState = Exclude<SpriteExpression, 'blink'>;

export interface SpriteManifest {
  id: string;
  name: string;
  /** Frame file names, relative to the manifest's own directory. `idle` is always present. */
  frames: { idle: string[] } & Partial<Record<SpriteExpression, string[]>>;
  /** Playback rate for multi-frame sequences. */
  fps: number;
  /** Normalised anchor inside the frame (0..1) that is pinned to the window's centre point. */
  anchor: [number, number];
  /** Size of the drawn creature relative to the window box. */
  scale: number;
}

/** Crossfade between two expressions (#19). Skipped entirely under `prefers-reduced-motion`. */
export const CROSSFADE_MS = 150;

export const SPRITE_PREFIX = 'sprite:';

export class SpriteManifestError extends Error {}

/** Ids and frame names end up in a URL under our own origin; keep them boring so no manifest can
 * walk out of its own directory (`..`, absolute paths, query strings). */
const SAFE_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

function isSafeSegment(value: string): boolean {
  return SAFE_SEGMENT.test(value) && !value.includes('..');
}

/** `sprite:placeholder` -> `placeholder`; anything else (including a bare id) -> null. */
export function spriteVariantId(variant: string | null | undefined): string | null {
  if (!variant || !variant.startsWith(SPRITE_PREFIX)) return null;
  const id = variant.slice(SPRITE_PREFIX.length);
  return isSafeSegment(id) ? id : null;
}

/** Directory a sprite set is served from, under the app's base path (`/pet/` in the core). */
export function spriteBaseUrl(base: string, id: string): string {
  const root = base.endsWith('/') ? base : `${base}/`;
  return `${root}variants/${id}/`;
}

export function manifestUrl(base: string, id: string): string {
  return `${spriteBaseUrl(base, id)}sprites.json`;
}

// ---- manifest validation -------------------------------------------------------------------------

function requireString(raw: Record<string, unknown>, key: string): string {
  const value = raw[key];
  if (typeof value !== 'string' || value.trim() === '') {
    throw new SpriteManifestError(`${key} must be a non-empty string`);
  }
  return value;
}

function parseFrameList(value: unknown, key: string): string[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new SpriteManifestError(`frames.${key} must be a non-empty array`);
  }
  return value.map((file) => {
    if (typeof file !== 'string' || !isSafeSegment(file)) {
      throw new SpriteManifestError(`frames.${key} contains an unusable file name`);
    }
    return file;
  });
}

/**
 * Validate an untrusted `sprites.json`. Throws `SpriteManifestError` with a reason a log line can
 * carry; the caller turns that into a fallback to the procedural variant.
 */
export function parseSpriteManifest(raw: unknown): SpriteManifest {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new SpriteManifestError('manifest must be a JSON object');
  }
  const obj = raw as Record<string, unknown>;

  const id = requireString(obj, 'id');
  if (!isSafeSegment(id)) throw new SpriteManifestError('id must be a plain path segment');
  const name = requireString(obj, 'name');

  const framesRaw = obj.frames;
  if (typeof framesRaw !== 'object' || framesRaw === null || Array.isArray(framesRaw)) {
    throw new SpriteManifestError('frames must be an object');
  }
  const framesObj = framesRaw as Record<string, unknown>;
  const unknown = Object.keys(framesObj).filter(
    (k) => !(SPRITE_EXPRESSIONS as readonly string[]).includes(k),
  );
  if (unknown.length > 0) {
    throw new SpriteManifestError(`frames has unknown expression(s): ${unknown.join(', ')}`);
  }
  if (!('idle' in framesObj)) throw new SpriteManifestError('frames.idle is required');
  const frames = { idle: parseFrameList(framesObj.idle, 'idle') } as SpriteManifest['frames'];
  for (const key of SPRITE_EXPRESSIONS) {
    if (key === 'idle' || !(key in framesObj)) continue;
    frames[key] = parseFrameList(framesObj[key], key);
  }

  const fps = obj.fps;
  if (typeof fps !== 'number' || !Number.isFinite(fps) || fps <= 0 || fps > 60) {
    throw new SpriteManifestError('fps must be a number in (0, 60]');
  }

  const anchor = obj.anchor;
  if (
    !Array.isArray(anchor) ||
    anchor.length !== 2 ||
    !anchor.every((v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1)
  ) {
    throw new SpriteManifestError('anchor must be two numbers in [0, 1]');
  }

  const scale = obj.scale;
  if (typeof scale !== 'number' || !Number.isFinite(scale) || scale <= 0 || scale > 4) {
    throw new SpriteManifestError('scale must be a number in (0, 4]');
  }

  return { id, name, frames, fps, anchor: [anchor[0], anchor[1]], scale };
}

/** Every frame file a manifest references, de-duplicated, in a stable order. */
export function manifestFiles(manifest: SpriteManifest): string[] {
  const seen = new Set<string>();
  for (const key of SPRITE_EXPRESSIONS) {
    for (const file of manifest.frames[key] ?? []) seen.add(file);
  }
  return [...seen];
}

// ---- state -> frames -----------------------------------------------------------------------------

/**
 * Project the pet state machine onto the sprite expressions.
 *
 * Order matters and is the honest one: an unknown connection beats everything (an offline pet must
 * not look like it is listening), then what Nox is *doing*, then how it feels. States no artist was
 * asked to draw (`error`, `muted`, `privacy`) resolve to `idle`; the capture indicator and the
 * status label — never colour-only, D238 — keep carrying those, exactly as for the procedural
 * renderer.
 */
export function spriteExpressionFor(input: PetInput): SpriteState {
  if (!input.connected) return 'sleeping';
  switch (input.functional) {
    case 'speaking':
      return 'speaking';
    case 'listening':
      return 'listening';
    case 'thinking':
    case 'working':
      return 'thinking';
    case 'unavailable':
      return 'sleeping';
    default:
      break;
  }
  if (input.expression === 'sleeping' || input.sleep === 'offline') return 'sleeping';
  return 'idle';
}

/**
 * Frames to play for an expression, with the set's own fallback chain. A set that only ships
 * `idle` is legal and plays `idle` for everything.
 */
export function framesFor(
  manifest: SpriteManifest,
  expression: SpriteExpression,
): { expression: SpriteExpression; files: string[] } {
  const own = manifest.frames[expression];
  if (own && own.length > 0) return { expression, files: own };
  return { expression: 'idle', files: manifest.frames.idle };
}

/** Which frame of a sequence is showing at time `tMs`. Single-frame sequences never advance. */
export function frameIndexAt(files: string[], fps: number, tMs: number): number {
  if (files.length <= 1) return 0;
  const step = 1000 / fps;
  return Math.floor(Math.max(0, tMs) / step) % files.length;
}

// ---- loading -------------------------------------------------------------------------------------

/** Loads one image URL; resolves on success, rejects on error. Injected so the loader is testable
 * without a DOM (the browser implementation lives in `SpritePet.tsx`). */
export type ImageLoader = (url: string) => Promise<unknown>;

export interface LoadedSprite {
  manifest: SpriteManifest;
  base: string;
  /** Absolute URL per frame file name. */
  urls: Record<string, string>;
}

export interface SpriteLoadOptions {
  fetchJson: (url: string) => Promise<unknown>;
  loadImage: ImageLoader;
  base: string;
}

export function reasonOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * Fetch + validate + preload one sprite set. Any failure — HTTP, malformed manifest, a single frame
 * that will not decode — throws `SpriteManifestError` with a reason; the caller logs it and falls
 * back to the procedural variant rather than showing a half-drawn creature (#19).
 */
export async function loadSprite(id: string, opts: SpriteLoadOptions): Promise<LoadedSprite> {
  let raw: unknown;
  try {
    raw = await opts.fetchJson(manifestUrl(opts.base, id));
  } catch (err) {
    throw new SpriteManifestError(`manifest unreachable: ${reasonOf(err)}`);
  }
  const manifest = parseSpriteManifest(raw);
  if (manifest.id !== id) {
    throw new SpriteManifestError(`manifest id "${manifest.id}" does not match directory "${id}"`);
  }
  const base = spriteBaseUrl(opts.base, id);
  const urls: Record<string, string> = {};
  const files = manifestFiles(manifest);
  const results = await Promise.allSettled(
    files.map((file) => {
      const url = `${base}${file}`;
      urls[file] = url;
      return opts.loadImage(url);
    }),
  );
  const failed = files.filter((_, i) => results[i].status === 'rejected');
  if (failed.length > 0) {
    throw new SpriteManifestError(`frame(s) failed to load: ${failed.join(', ')}`);
  }
  return { manifest, base, urls };
}
