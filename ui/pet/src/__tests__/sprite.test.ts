/**
 * Sprite variant (#19) and live variant switching (#24) — the parts that are pure functions and can
 * therefore be checked without a DOM (vitest runs in the `node` environment, see vite.config.ts).
 *
 * Also asserts the committed placeholder set against the same validator the browser uses, so a
 * hand-edited `sprites.json` or a renamed PNG fails here rather than in the pet window.
 */

import { describe, expect, it } from 'vitest';

import placeholderJson from '../../public/variants/placeholder/sprites.json';

import { petVariantChanged, variantFromStateReply } from '../ipc';
import { DEFAULT_MOOD, type Functional, type PetInput } from '../petState';
import {
  CROSSFADE_MS,
  type SpriteManifest,
  SpriteManifestError,
  frameIndexAt,
  framesFor,
  loadSprite,
  manifestFiles,
  manifestUrl,
  parseSpriteManifest,
  spriteBaseUrl,
  spriteExpressionFor,
  spriteVariantId,
} from '../variants/sprite';

/** Every PNG that is actually committed next to the manifest (no node:fs — the pet package has no
 * @types/node, and this keeps the test runnable in vitest's `node` environment either way). */
const PLACEHOLDER_PNGS = new Set(
  Object.keys(import.meta.glob('../../public/variants/placeholder/*.png')).map(
    (path) => path.split('/').pop() as string,
  ),
);

const VALID = {
  id: 'placeholder',
  name: 'Placeholder',
  frames: { idle: ['idle.png'], speaking: ['a.png', 'b.png'] },
  fps: 8,
  anchor: [0.5, 0.56],
  scale: 1,
};

const baseInput: PetInput = {
  connected: true,
  functional: 'idle',
  expression: 'normal',
  intensity: 0.8,
  mood: DEFAULT_MOOD,
  sleep: 'none',
  speakingLevel: 0,
};

describe('variant id parsing', () => {
  it('only treats a sprite: prefix as a sprite set, leaving procedural ids alone', () => {
    expect(spriteVariantId('sprite:placeholder')).toBe('placeholder');
    expect(spriteVariantId('fox')).toBeNull();
    expect(spriteVariantId(null)).toBeNull();
    expect(spriteVariantId('')).toBeNull();
  });

  it('refuses ids that would escape the variant directory', () => {
    expect(spriteVariantId('sprite:../../etc')).toBeNull();
    expect(spriteVariantId('sprite:a/b')).toBeNull();
    expect(spriteVariantId('sprite:')).toBeNull();
  });

  it('builds the manifest URL under the app base path', () => {
    expect(spriteBaseUrl('/pet/', 'placeholder')).toBe('/pet/variants/placeholder/');
    expect(spriteBaseUrl('/pet', 'placeholder')).toBe('/pet/variants/placeholder/');
    expect(manifestUrl('/pet/', 'placeholder')).toBe('/pet/variants/placeholder/sprites.json');
  });
});

describe('manifest validation', () => {
  it('accepts a well-formed manifest', () => {
    const m = parseSpriteManifest(VALID);
    expect(m.id).toBe('placeholder');
    expect(m.frames.idle).toEqual(['idle.png']);
    expect(m.anchor).toEqual([0.5, 0.56]);
  });

  const bad: [string, unknown][] = [
    ['not an object', 'nope'],
    ['an array', []],
    ['a missing id', { ...VALID, id: undefined }],
    ['a path-like id', { ...VALID, id: '../evil' }],
    ['a missing name', { ...VALID, name: '' }],
    ['missing frames', { ...VALID, frames: undefined }],
    ['frames without idle', { ...VALID, frames: { speaking: ['a.png'] } }],
    ['an empty frame list', { ...VALID, frames: { idle: [] } }],
    ['a traversing frame name', { ...VALID, frames: { idle: ['../secret.png'] } }],
    ['an absolute frame name', { ...VALID, frames: { idle: ['/etc/passwd'] } }],
    ['an unknown expression', { ...VALID, frames: { ...VALID.frames, dancing: ['a.png'] } }],
    ['fps of zero', { ...VALID, fps: 0 }],
    ['fps above 60', { ...VALID, fps: 120 }],
    ['a three-element anchor', { ...VALID, anchor: [0.5, 0.5, 0.5] }],
    ['an anchor outside 0..1', { ...VALID, anchor: [0.5, 2] }],
    ['a scale of zero', { ...VALID, scale: 0 }],
  ];
  for (const [what, value] of bad) {
    it(`rejects ${what}`, () => {
      expect(() => parseSpriteManifest(value)).toThrow(SpriteManifestError);
    });
  }

  it('lists every referenced file once', () => {
    const m = parseSpriteManifest({ ...VALID, frames: { idle: ['a.png'], blink: ['a.png', 'b.png'] } });
    expect(manifestFiles(m)).toEqual(['a.png', 'b.png']);
  });
});

describe('committed placeholder set', () => {
  const manifest: SpriteManifest = parseSpriteManifest(placeholderJson);

  it('validates and declares every expression the renderer maps onto', () => {
    expect(manifest.id).toBe('placeholder');
    for (const key of ['idle', 'listening', 'speaking', 'sleeping', 'thinking', 'blink'] as const) {
      expect(manifest.frames[key]?.length ?? 0).toBeGreaterThan(0);
    }
  });

  it('references exactly the PNGs that are committed beside it', () => {
    const referenced = manifestFiles(manifest);
    for (const file of referenced) {
      expect(PLACEHOLDER_PNGS.has(file), `${file} is referenced but not committed`).toBe(true);
    }
    expect([...PLACEHOLDER_PNGS].sort()).toEqual([...referenced].sort());
  });
});

describe('state -> frame mapping', () => {
  const manifest = parseSpriteManifest({
    ...VALID,
    frames: {
      idle: ['idle.png'],
      listening: ['l0.png', 'l1.png'],
      speaking: ['s0.png', 's1.png', 's2.png'],
      thinking: ['t.png'],
      sleeping: ['z.png'],
    },
  });

  const cases: [Functional, string][] = [
    ['speaking', 'speaking'],
    ['listening', 'listening'],
    ['thinking', 'thinking'],
    ['working', 'thinking'],
    ['unavailable', 'sleeping'],
    ['idle', 'idle'],
    ['error', 'idle'],
    ['muted', 'idle'],
    ['privacy', 'idle'],
  ];
  for (const [functional, expected] of cases) {
    it(`maps functional ${functional} to ${expected}`, () => {
      expect(spriteExpressionFor({ ...baseInput, functional })).toBe(expected);
    });
  }

  it('sleeps when disconnected, whatever the last known state said', () => {
    expect(spriteExpressionFor({ ...baseInput, connected: false, functional: 'speaking' })).toBe(
      'sleeping',
    );
  });

  it('sleeps on the sleeping expression and on the offline sleep tier', () => {
    expect(spriteExpressionFor({ ...baseInput, expression: 'sleeping' })).toBe('sleeping');
    expect(spriteExpressionFor({ ...baseInput, sleep: 'offline' })).toBe('sleeping');
  });

  it('falls back to idle for an expression the set does not ship', () => {
    const minimal = parseSpriteManifest({ ...VALID, frames: { idle: ['idle.png'] } });
    expect(framesFor(minimal, 'speaking')).toEqual({ expression: 'idle', files: ['idle.png'] });
    expect(framesFor(manifest, 'speaking').expression).toBe('speaking');
  });

  it('cycles multi-frame sequences at the manifest fps and holds single frames', () => {
    const files = ['a.png', 'b.png', 'c.png'];
    expect(frameIndexAt(files, 8, 0)).toBe(0);
    expect(frameIndexAt(files, 8, 130)).toBe(1);
    expect(frameIndexAt(files, 8, 260)).toBe(2);
    expect(frameIndexAt(files, 8, 380)).toBe(0);
    expect(frameIndexAt(['only.png'], 8, 99999)).toBe(0);
  });

  it('keeps the crossfade at the 150 ms the issue asks for', () => {
    expect(CROSSFADE_MS).toBe(150);
  });
});

describe('live variant switching (#24)', () => {
  const env = (name: string, payload: Record<string, unknown>) =>
    ({ v: 1, id: 'x', kind: 'event', name, src: { role: 'core', id: 'core' }, payload }) as never;

  it('reacts only to settings.changed mentioning pet.variant', () => {
    expect(petVariantChanged(env('settings.changed', { paths: ['pet.variant'] }))).toBe(true);
    expect(petVariantChanged(env('settings.changed', { paths: ['ui.language', 'pet.variant'] }))).toBe(
      true,
    );
    expect(petVariantChanged(env('settings.changed', { paths: ['ui.language'] }))).toBe(false);
    expect(petVariantChanged(env('settings.changed', {}))).toBe(false);
    expect(petVariantChanged(env('state.changed', { paths: ['pet.variant'] }))).toBe(false);
  });

  it('reads the new variant out of either state.get reply shape', () => {
    expect(variantFromStateReply({ path: 'pet.variant', value: 'sprite:placeholder' })).toBe(
      'sprite:placeholder',
    );
    expect(variantFromStateReply({ pet: { variant: 'fox' } })).toBe('fox');
  });

  it('returns null for the snapshot the core actually sends the pet role today', () => {
    expect(variantFromStateReply({ assistant: {}, privacy: {}, system: {} })).toBeNull();
    expect(variantFromStateReply({ path: 'pet.variant', value: '' })).toBeNull();
    expect(variantFromStateReply(null)).toBeNull();
    expect(variantFromStateReply('fox')).toBeNull();
  });
});

describe('loading and fallback', () => {
  const ok = async (url: string) => ({ url });
  const manifestOf = (body: unknown) => async () => body;

  it('loads a set whose frames all decode', async () => {
    const loaded = await loadSprite('placeholder', {
      base: '/pet/',
      fetchJson: manifestOf(VALID),
      loadImage: ok,
    });
    expect(loaded.manifest.name).toBe('Placeholder');
    expect(loaded.urls['idle.png']).toBe('/pet/variants/placeholder/idle.png');
  });

  it('names the frames that failed, so the fallback is logged with a reason', async () => {
    await expect(
      loadSprite('placeholder', {
        base: '/pet/',
        fetchJson: manifestOf(VALID),
        loadImage: async (url: string) => {
          if (url.endsWith('b.png')) throw new Error('404');
          return { url };
        },
      }),
    ).rejects.toThrow(/b\.png/);
  });

  it('reports an unreachable manifest rather than throwing a bare network error', async () => {
    await expect(
      loadSprite('placeholder', {
        base: '/pet/',
        fetchJson: async () => {
          throw new Error('HTTP 404');
        },
        loadImage: ok,
      }),
    ).rejects.toThrow(/manifest unreachable: HTTP 404/);
  });

  it('refuses a manifest whose id does not match the directory it came from', async () => {
    await expect(
      loadSprite('placeholder', {
        base: '/pet/',
        fetchJson: manifestOf({ ...VALID, id: 'somethingelse' }),
        loadImage: ok,
      }),
    ).rejects.toThrow(SpriteManifestError);
  });
});
