/**
 * The relief renderer under bad conditions: clocks that jump backwards, NaN in a pose, a broken or
 * hostile rig file, a GPU that fails half-way through setup, settings that change while it runs.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { DEFAULT_MOOD, type PetInput, deriveAnim } from '../petState';
import { ReliefBrain } from '../relief/brain';
import { FrameClock, MAX_STEP_S, watchMedia, watchPixelRatio } from '../relief/clock';
import type { ReliefGeometry } from '../relief/mesh';
import { MAX_DAMPING, parseReliefDefinition } from '../relief/reliefFile';
import { ReliefRenderer, ReliefRendererError } from '../relief/renderer';
import { type BoneDef, Skeleton } from '../relief/skeleton';
import { reliefTargets } from '../relief/stateMapping';

const RAW = JSON.parse(
  readFileSync(join(process.cwd(), 'public', 'variants', 'eulenfuchs', 'relief', 'relief.json'), 'utf-8'),
) as Record<string, unknown>;
const DEF = parseReliefDefinition(RAW, 'eulenfuchs');
const clone = (): Record<string, unknown> => JSON.parse(JSON.stringify(RAW));

describe('frame clock', () => {
  it('never steps backwards when a timestamp arrives late', () => {
    const c = new FrameClock(1000);
    c.tick(1016);
    const back = c.tick(990); // e.g. a visibilitychange timestamp older than the last frame
    expect(back.dt).toBe(0);
    const next = c.tick(1032);
    expect(next.dt).toBeCloseTo(0.016, 6); // measured from the newest frame, not from 990
    expect(next.t).toBeGreaterThanOrEqual(back.t);
  });

  it('caps a long gap and cuts hidden time out of the animation', () => {
    const c = new FrameClock(0);
    expect(c.tick(5000).dt).toBe(MAX_STEP_S);
    c.resume(65_000); // a minute hidden
    expect(c.tick(65_016).t).toBeCloseTo(5.016, 6);
    expect(c.tick(Number.NaN).dt).toBe(0);
  });
});

describe('live media settings', () => {
  afterEach(() => vi.unstubAllGlobals());

  function fakeMatchMedia() {
    const lists = new Map<string, Set<(e: { matches: boolean }) => void>>();
    const matchMedia = (query: string) => ({
      matches: false,
      addEventListener: (_: string, fn: (e: { matches: boolean }) => void) => {
        if (!lists.has(query)) lists.set(query, new Set());
        lists.get(query)!.add(fn);
      },
      removeEventListener: (_: string, fn: (e: { matches: boolean }) => void) => lists.get(query)?.delete(fn),
    });
    const fire = (query: string, matches: boolean) => [...(lists.get(query) ?? [])].forEach((fn) => fn({ matches }));
    const count = () => [...lists.values()].reduce((n, s) => n + s.size, 0);
    return { matchMedia, fire, count };
  }

  it('follows prefers-reduced-motion while running and stops listening on unmount', () => {
    const fake = fakeMatchMedia();
    vi.stubGlobal('matchMedia', fake.matchMedia);
    const seen: boolean[] = [];
    const stop = watchMedia('(prefers-reduced-motion: reduce)', (on) => seen.push(on));
    fake.fire('(prefers-reduced-motion: reduce)', true);
    expect(seen).toEqual([true]);
    stop();
    expect(fake.count()).toBe(0);
  });

  it('re-arms the pixel-ratio watch after every change', () => {
    const fake = fakeMatchMedia();
    vi.stubGlobal('matchMedia', fake.matchMedia);
    vi.stubGlobal('devicePixelRatio', 1);
    const ratios: number[] = [];
    const stop = watchPixelRatio((r) => ratios.push(r));
    vi.stubGlobal('devicePixelRatio', 1.5);
    fake.fire('(resolution: 1dppx)', false);
    vi.stubGlobal('devicePixelRatio', 2);
    fake.fire('(resolution: 1.5dppx)', false);
    expect(ratios).toEqual([1.5, 2]);
    expect(fake.count()).toBe(1);
    stop();
    expect(fake.count()).toBe(0);
  });
});

describe('numeric safety', () => {
  const BONES: BoneDef[] = [
    { name: 'root', parent: null, pivot: [0, 0], tip: [0, -50] },
    { name: 'ear', parent: 'root', pivot: [0, -50], tip: [0, -90], jiggle: { stiffness: 260, damping: 9, limit: 0.5, gravity: 0 } },
  ];

  it('recovers a spring from a NaN pose instead of spreading it', () => {
    const s = new Skeleton(BONES);
    s.update(0);
    s.get('root').x = Number.NaN;
    s.update(1 / 60);
    s.resetPose();
    for (let i = 0; i < 3; i++) s.update(1 / 60);
    expect(Number.isFinite(s.springOffset('ear'))).toBe(true);
    expect(s.world.every((m) => m.every(Number.isFinite))).toBe(true);
  });

  it('treats a negative, NaN or huge step in the brain as a harmless one', () => {
    const input: PetInput = {
      connected: true, functional: 'idle', expression: 'happy', intensity: 1, mood: DEFAULT_MOOD,
      sleep: 'none', speakingLevel: 0, feeds: 0,
    };
    const skel = new Skeleton(DEF.bones);
    const brain = new ReliefBrain(DEF, 1);
    const target = reliefTargets(input, deriveAnim(input), false);
    brain.update(0, 0, skel, target);
    for (const dt of [-1, Number.NaN, 1e6, Number.POSITIVE_INFINITY]) {
      const face = brain.update(1, dt, skel, target);
      expect(Object.values(face).flat().every((v) => typeof v !== 'number' || Number.isFinite(v))).toBe(true);
    }
    expect(skel.world.every((m) => m.every(Number.isFinite))).toBe(true);
  });

  it('refuses spring damping the fixed physics step cannot integrate', () => {
    const raw = clone();
    const bone = (raw.bones as { jiggle?: { damping: number } }[]).find((b) => b.jiggle)!;
    bone.jiggle!.damping = MAX_DAMPING + 1;
    expect(() => parseReliefDefinition(raw, 'eulenfuchs')).toThrow(/damping/);
  });

  it('refuses a focal length too close to the deepest relief', () => {
    const raw = clone();
    raw.focal = 150; // head: z 22 + relief 58 = 80, so the focal length must exceed 160
    expect(() => parseReliefDefinition(raw, 'eulenfuchs')).toThrow(/focal/);
    raw.focal = 161;
    expect(parseReliefDefinition(raw, 'eulenfuchs').focal).toBe(161);
  });
});

describe('renderer setup on a failing GPU', () => {
  /** A WebGL2 stand-in that records calls; `fail` makes one method return null / false. */
  function fakeGl(fail: { method: string; after?: number } | null) {
    const calls: string[] = [];
    const counts: Record<string, number> = {};
    let next = 1;
    const gl = new Proxy({} as Record<string, unknown>, {
      get(_, key) {
        if (typeof key !== 'string') return undefined;
        if (/^[A-Z0-9_]+$/.test(key)) return key; // constants
        return (...args: unknown[]) => {
          calls.push(key);
          counts[key] = (counts[key] ?? 0) + 1;
          if (fail && key === fail.method && counts[key] > (fail.after ?? 0)) return key.startsWith('get') ? false : null;
          if (key === 'isContextLost') return false;
          if (key.startsWith('get') && key.endsWith('Parameter')) return true;
          if (key === 'getUniformLocation') return { id: next++ };
          if (key.startsWith('create')) return { id: next++, kind: key };
          return args.length ? undefined : undefined;
        };
      },
    });
    const canvas = {
      getContext: (type: string) => (type === 'webgl2' ? gl : null),
      addEventListener: () => {},
      removeEventListener: () => {},
      width: 0,
      height: 0,
    } as unknown as HTMLCanvasElement;
    return { canvas, calls, counts };
  }

  const geo: ReliefGeometry = {
    vertices: new Float32Array(17 * 3),
    indices: new Uint32Array([0, 1, 2]),
    ranges: [{ name: 'body', first: 0, count: 3 }],
    eyeVertices: new Float32Array(0),
    eyeIndices: new Uint32Array(0),
    eyes: [],
    vertexCount: 3,
  };
  const atlas = {} as TexImageSource;

  it('frees the shader objects once a program is linked', () => {
    const { canvas, counts } = fakeGl(null);
    const r = new ReliefRenderer(canvas, DEF, geo, new Skeleton(DEF.bones), atlas);
    expect(counts.createShader).toBe(8);
    expect(counts.deleteShader).toBe(8);
    expect(counts.detachShader).toBe(8);
    r.dispose();
    expect(counts.deleteProgram).toBe(4);
  });

  it('frees everything it made when a shader does not compile', () => {
    const { canvas, counts } = fakeGl({ method: 'getShaderParameter', after: 3 });
    expect(() => new ReliefRenderer(canvas, DEF, geo, new Skeleton(DEF.bones), atlas)).toThrow(ReliefRendererError);
    expect(counts.deleteShader).toBe(counts.createShader);
    expect(counts.deleteProgram).toBe(counts.createProgram);
  });

  it('frees programs and buffers when the GPU runs out half-way', () => {
    const { canvas, counts } = fakeGl({ method: 'createVertexArray', after: 1 });
    expect(() => new ReliefRenderer(canvas, DEF, geo, new Skeleton(DEF.bones), atlas)).toThrow(ReliefRendererError);
    expect(counts.deleteProgram).toBe(4);
    expect(counts.deleteBuffer).toBe(counts.createBuffer);
    expect(counts.deleteVertexArray).toBe(1);
  });
});
