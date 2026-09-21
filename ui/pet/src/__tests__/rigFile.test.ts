/**
 * `rig.json` validation, loading, and the fallback that keeps the pet on screen when it fails.
 *
 * The rule the whole feature rests on: a rigged variant is an *upgrade*. No `rig.json` is normal,
 * a broken one is a logged reason and the static frames, and a missing key-pose frame is a gap
 * that is reported rather than faked. Each of those is a test here, because each of them is a way
 * the pet could quietly disappear instead.
 */

import { describe, expect, it } from 'vitest';

import { loadRig, parseRigDefinition, RigDefinitionError, rigFiles, rigUrl } from '../rig/rigFile';
import { planFor } from '../rig/stateMapping';
import type { PetInput } from '../petState';
import { DEFAULT_MOOD } from '../petState';

const MINIMAL = {
  id: 'meereswolf',
  base: 'base.png',
  bones: [
    { name: 'root', parent: null, pivot: [0.5, 0.9], influence: { radius: 2 } },
    { name: 'head', parent: 'root', pivot: [0.5, 0.3], restAngle: -90, influence: { radius: 0.3 } },
    { name: 'lid.l', parent: 'head', pivot: [0.45, 0.28], channelOnly: true, influence: { radius: 0.01 } },
  ],
  layers: [
    {
      name: 'eye.l',
      file: 'eye_l.png',
      rect: [0.4, 0.2, 0.5, 0.3],
      grid: { columns: 3, rows: 3 },
      lid: { bone: 'lid.l' },
    },
  ],
  clips: {
    breathe: {
      durationMs: 5000,
      loop: true,
      tracks: [
        {
          bone: 'head',
          channel: 'scaleY',
          keys: [
            { t: 0, value: 1 },
            { t: 1, value: 1.02 },
          ],
        },
      ],
    },
  },
  poses: { curl: { file: 'curl.png' } },
  mesh: { columns: 8, rows: 8 },
};

function withRig(mutate: (rig: Record<string, unknown>) => void): Record<string, unknown> {
  const copy = JSON.parse(JSON.stringify(MINIMAL)) as Record<string, unknown>;
  mutate(copy);
  return copy;
}

function input(overrides: Partial<PetInput> = {}): PetInput {
  return {
    connected: true,
    functional: 'idle',
    expression: 'normal',
    intensity: 1,
    mood: DEFAULT_MOOD,
    sleep: 'none',
    speakingLevel: 0,
    ...overrides,
  };
}

describe('rig definition validation', () => {
  it('accepts the shape the Meereswolf rig uses', () => {
    const rig = parseRigDefinition(MINIMAL, 'meereswolf');
    expect(rig.bones).toHaveLength(3);
    expect(rig.bones[2].channelOnly).toBe(true);
    expect(rig.layers[0].lid?.bone).toBe('lid.l');
    expect(rig.clips.breathe.loop).toBe(true);
    expect(rig.assetDir).toBe('rig');
  });

  it('refuses a rig whose id does not match the directory it was served from', () => {
    expect(() => parseRigDefinition(MINIMAL, 'someone_else')).toThrow(/does not match the directory/);
  });

  it('names the field when a number is out of range', () => {
    expect(() => parseRigDefinition(withRig((r) => { (r.mesh as Record<string, number>).columns = 500; }), 'meereswolf')).toThrow(
      /mesh\.columns: must be between/,
    );
  });

  it('refuses a file name that could walk out of the variant directory', () => {
    expect(() => parseRigDefinition(withRig((r) => { r.base = '../../secrets.png'; }), 'meereswolf')).toThrow(
      /base: must be a plain file or directory name/,
    );
  });

  it('refuses a track pointing at a bone that does not exist', () => {
    expect(() =>
      parseRigDefinition(
        withRig((r) => {
          const clips = r.clips as Record<string, { tracks: { bone: string }[] }>;
          clips.breathe.tracks[0].bone = 'wing';
        }),
        'meereswolf',
      ),
    ).toThrow(/no bone named "wing"/);
  });

  it('refuses a lid pointing at a bone that does not exist', () => {
    expect(() =>
      parseRigDefinition(
        withRig((r) => {
          const layers = r.layers as { lid: { bone: string } }[];
          layers[0].lid.bone = 'nostril';
        }),
        'meereswolf',
      ),
    ).toThrow(/no bone named "nostril"/);
  });

  it('refuses a parent that is defined below its child', () => {
    expect(() =>
      parseRigDefinition(
        withRig((r) => {
          const bones = r.bones as { name: string; parent: string | null }[];
          bones[1].parent = 'lid.l';
        }),
        'meereswolf',
      ),
    ).toThrow(/is not defined above it/);
  });

  it('refuses more than one root', () => {
    expect(() =>
      parseRigDefinition(
        withRig((r) => {
          const bones = r.bones as { parent: string | null }[];
          bones[1].parent = null;
        }),
        'meereswolf',
      ),
    ).toThrow(/exactly one bone must have parent null/);
  });

  it('refuses keyframes that are not strictly ascending from zero', () => {
    expect(() =>
      parseRigDefinition(
        withRig((r) => {
          const clips = r.clips as Record<string, { tracks: { keys: { t: number }[] }[] }>;
          clips.breathe.tracks[0].keys = [
            { t: 0.2, value: 1 },
            { t: 1, value: 1 },
          ] as { t: number; value: number }[];
        }),
        'meereswolf',
      ),
    ).toThrow(/first keyframe must be at t = 0/);
  });

  it('refuses an easing it cannot apply instead of silently using another', () => {
    expect(() =>
      parseRigDefinition(
        withRig((r) => {
          const clips = r.clips as Record<string, { easing: string }>;
          clips.breathe.easing = 'bouncy';
        }),
        'meereswolf',
      ),
    ).toThrow(/unknown easing "bouncy"/);
  });

  it('refuses a rect whose right edge is not right of its left', () => {
    expect(() =>
      parseRigDefinition(
        withRig((r) => {
          const layers = r.layers as { rect: number[] }[];
          layers[0].rect = [0.5, 0.2, 0.4, 0.3];
        }),
        'meereswolf',
      ),
    ).toThrow(/right\/bottom must exceed left\/top/);
  });

  it('refuses anything that is not a JSON object at all', () => {
    expect(() => parseRigDefinition([1, 2, 3], 'meereswolf')).toThrow(RigDefinitionError);
    expect(() => parseRigDefinition(null, 'meereswolf')).toThrow(/must be a JSON object/);
  });

  it('lists every file it needs once, in a stable order', () => {
    expect(rigFiles(parseRigDefinition(MINIMAL, 'meereswolf'))).toEqual([
      'base.png',
      'eye_l.png',
      'curl.png',
    ]);
  });

  it('builds URLs under the variant directory', () => {
    const rig = parseRigDefinition(MINIMAL, 'meereswolf');
    expect(rigUrl('/pet/variants/meereswolf/', rig, 'base.png')).toBe(
      '/pet/variants/meereswolf/rig/base.png',
    );
  });
});

describe('loading a rig', () => {
  const stubImage = {} as TexImageSource;
  const options = (overrides: Partial<Parameters<typeof loadRig>[1]> = {}) => ({
    variantBase: '/pet/variants/meereswolf/',
    fetchJson: async () => MINIMAL,
    loadImage: async () => stubImage,
    ...overrides,
  });

  it('loads the textures and reports no gaps when all the art exists', async () => {
    const loaded = await loadRig('meereswolf', options());
    expect(loaded?.missingPoses).toEqual([]);
    expect(Object.keys(loaded?.images ?? {}).sort()).toEqual(['base.png', 'curl.png', 'eye_l.png']);
  });

  it('reports "no rig" rather than an error when the variant simply has none', async () => {
    const fromMissingFile = await loadRig('meereswolf', options({ fetchJson: async () => { throw new Error('HTTP 404'); } }));
    expect(fromMissingFile).toBeNull();
    const fromNullBody = await loadRig('meereswolf', options({ fetchJson: async () => null }));
    expect(fromNullBody).toBeNull();
  });

  it('fails loudly when a texture the mesh needs will not decode', async () => {
    await expect(
      loadRig(
        'meereswolf',
        options({
          loadImage: async (url: string) => {
            if (url.endsWith('eye_l.png')) throw new Error('decode failed');
            return stubImage;
          },
        }),
      ),
    ).rejects.toThrow(/texture\(s\) failed to load: eye_l\.png/);
  });

  it('treats a key pose whose art does not exist yet as a named gap, not a failure', async () => {
    const loaded = await loadRig(
      'meereswolf',
      options({
        loadImage: async (url: string) => {
          if (url.endsWith('curl.png')) throw new Error('HTTP 404');
          return stubImage;
        },
      }),
    );
    expect(loaded?.missingPoses).toEqual(['curl']);
    expect(loaded?.images['curl.png']).toBeUndefined();
    expect(loaded?.images['base.png']).toBe(stubImage);
  });
});

describe('pet state mapped onto the rig', () => {
  it('breathes and sways when idle, and allows unprompted motion', () => {
    const plan = planFor(input(), false);
    expect(plan.sustained.map((clip) => clip.name)).toContain('breathe');
    expect(plan.sustained.map((clip) => clip.name)).toContain('tail_sway');
    expect(plan.ambient).toBe('full');
    expect(plan.lidClosure).toBe(0);
  });

  it('perks up and tilts its head while listening', () => {
    const names = planFor(input({ functional: 'listening' }), false).sustained.map((c) => c.name);
    expect(names).toContain('perk');
    expect(names).toContain('head_tilt');
  });

  it('bobs harder the louder the speech is, and only blinks while it talks', () => {
    const quiet = planFor(input({ functional: 'speaking', speakingLevel: 0 }), false);
    const loud = planFor(input({ functional: 'speaking', speakingLevel: 1 }), false);
    const weightOf = (plan: typeof quiet) =>
      plan.sustained.find((clip) => clip.name === 'speak_idle')?.weight ?? 0;
    expect(weightOf(loud)).toBeGreaterThan(weightOf(quiet));
    expect(loud.ambient).toBe('blink-only');
  });

  it('shuts its eyes and breathes slowly when it is asleep or unreachable', () => {
    for (const asleep of [
      input({ expression: 'sleeping' }),
      input({ connected: false }),
      input({ functional: 'unavailable' }),
      input({ sleep: 'offline' }),
    ]) {
      const plan = planFor(asleep, false);
      expect(plan.sustained.map((clip) => clip.name)).toContain('sleep_breathe');
      expect(plan.lidClosure).toBe(1);
      expect(plan.ambient).toBe('none');
    }
  });

  it('drops to breathing alone under prefers-reduced-motion, but keeps the mood', () => {
    const plan = planFor(input({ expression: 'sad' }), true);
    expect(plan.sustained.map((clip) => clip.name)).toEqual(['breathe']);
    expect(plan.ambient).toBe('none');
    expect(plan.posture['ear.l'].rotate).toBeGreaterThan(0);
  });

  it('scales a posture by how strongly the mood is felt', () => {
    const faint = planFor(input({ expression: 'sad', intensity: 0.25 }), false);
    const full = planFor(input({ expression: 'sad', intensity: 1 }), false);
    expect(faint.posture['ear.l'].rotate).toBeCloseTo(full.posture['ear.l'].rotate * 0.25, 5);
  });

  it('holds no posture for a mood a body cannot show', () => {
    expect(planFor(input({ expression: 'normal' }), false).posture).toEqual({});
    expect(planFor(input({ expression: 'not_a_mood' }), false).posture).toEqual({});
  });

  it('asks only for clips the shared clip list names, so a rig knows what to author', async () => {
    const { RIG_CLIPS } = await import('../rig/stateMapping');
    const states: PetInput[] = [
      input(),
      input({ functional: 'listening' }),
      input({ functional: 'thinking' }),
      input({ functional: 'working' }),
      input({ functional: 'speaking' }),
      input({ connected: false }),
    ];
    for (const state of states) {
      for (const clip of planFor(state, false).sustained) {
        expect(RIG_CLIPS).toContain(clip.name);
      }
    }
  });
});
