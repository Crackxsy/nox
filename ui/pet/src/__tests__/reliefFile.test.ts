/**
 * The relief rig file and its feature flag: what ships parses, what is broken is refused with a
 * message naming the field, and only converted creatures switch renderer.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { buildGeometry, weightFn } from '../relief/mesh';
import type { Pixels } from '../relief/mesh';
import { ReliefDefinitionError, parseReliefDefinition } from '../relief/reliefFile';
import { Skeleton } from '../relief/skeleton';
import { parseRigDefinition } from '../rig/rigFile';

const VARIANTS = join(process.cwd(), 'public', 'variants');
const read = (...p: string[]): unknown => JSON.parse(readFileSync(join(VARIANTS, ...p), 'utf-8'));
const RAW = read('eulenfuchs', 'relief', 'relief.json') as Record<string, unknown>;
const clone = (): Record<string, unknown> => JSON.parse(JSON.stringify(RAW));

describe('relief.json', () => {
  it('parses the shipped eulenfuchs rig', () => {
    const def = parseReliefDefinition(RAW, 'eulenfuchs');
    expect(def.bones).toHaveLength(22);
    expect(def.order).toContain('#eyes');
    expect(def.roles.tails).toHaveLength(2);
  });

  it('names the field that is wrong', () => {
    const wrongId = () => parseReliefDefinition(RAW, 'chamster');
    expect(wrongId).toThrow(ReliefDefinitionError);
    expect(wrongId).toThrow(/id/);

    const badBone = clone();
    (badBone.parts as { weights: unknown }[])[0].weights = { kind: 'rigid', bone: 'tentacle' };
    expect(() => parseReliefDefinition(badBone, 'eulenfuchs')).toThrow(/parts\[0\]\.weights\.bone.*tentacle/);

    const missing = clone();
    missing.order = (missing.order as string[]).filter((n) => n !== 'jaw');
    expect(() => parseReliefDefinition(missing, 'eulenfuchs')).toThrow(/order.*jaw/);

    const mismatch = clone();
    (mismatch.parts as { atlas: number[] }[])[1].atlas[2] += 1;
    expect(() => parseReliefDefinition(mismatch, 'eulenfuchs')).toThrow(/same size/);

    const noJaw = clone();
    noJaw.bones = (noJaw.bones as { name: string }[]).filter((b) => b.name !== 'jaw');
    expect(() => parseReliefDefinition(noJaw, 'eulenfuchs')).toThrow(/jaw/);
  });
});

describe('relief feature flag in rig.json', () => {
  const rigged = readdirSync(VARIANTS).filter((v) => existsSync(join(VARIANTS, v, 'rig.json')));

  it('is on for the eulenfuchs, and only for creatures that ship relief assets', () => {
    const on = rigged.filter((v) => parseRigDefinition(read(v, 'rig.json'), v).relief?.enabled);
    expect(on).toContain('eulenfuchs');
    for (const v of rigged) {
      if (!on.includes(v)) continue;
      expect(existsSync(join(VARIANTS, v, 'relief', 'relief.json')), v).toBe(true);
    }
    // the others keep the older rig renderer until they are converted
    expect(on.length).toBeLessThan(rigged.length);
  });

  it('points every enabled variant at relief assets that exist', () => {
    for (const v of rigged) {
      const flag = parseRigDefinition(read(v, 'rig.json'), v).relief;
      if (!flag?.enabled) continue;
      const dir = join(VARIANTS, v, flag.dir);
      const def = parseReliefDefinition(read(v, flag.dir, 'relief.json'), v);
      expect(existsSync(join(dir, def.atlas))).toBe(true);
      expect(existsSync(join(dir, def.depth))).toBe(true);
    }
  });

  it('refuses a relief directory that walks out of the variant', () => {
    const rig = read('eulenfuchs', 'rig.json') as Record<string, unknown>;
    expect(() => parseRigDefinition({ ...rig, relief: { dir: '../chamster' } }, 'eulenfuchs')).toThrow(
      /relief\.dir/,
    );
    expect(
      parseRigDefinition({ ...rig, relief: { dir: 'relief', enabled: false } }, 'eulenfuchs').relief?.enabled,
    ).toBe(false);
  });
});

describe('relief geometry', () => {
  it('splits skin weights along a tail: base at the root, tip bone at the end', () => {
    const fn = weightFn({
      kind: 'chain',
      points: [
        [0, 0],
        [100, 0],
      ],
      bones: ['t0', 't1'],
      base: 'hips',
      baseLen: 0.1,
    });
    const at = (x: number) => Object.fromEntries(fn(x, 0));
    expect(at(0).hips).toBeCloseTo(1);
    expect(at(100).t1).toBeCloseTo(1);
    for (const x of [0, 13, 37, 50, 71, 100]) {
      const sum = fn(x, 0).reduce((s, [, w]) => s + w, 0);
      expect(sum).toBeCloseTo(1, 6);
    }
  });

  it('builds a mesh that skips empty space and turns the head with the head bones only', () => {
    const def = parseReliefDefinition(RAW, 'eulenfuchs');
    const skel = new Skeleton(def.bones);
    // a fully opaque atlas and a flat depth map: every cell is kept, every normal faces the viewer
    const w = 1024;
    const h = 1024;
    const opaque: Pixels = { data: new Uint8ClampedArray(w * h * 4).fill(255), width: w, height: h };
    const flat: Pixels = { data: new Uint8ClampedArray(w * h * 4), width: w, height: h };
    const full = buildGeometry(def, skel, opaque, flat);
    const empty: Pixels = { data: new Uint8ClampedArray(w * h * 4), width: w, height: h };
    const culled = buildGeometry(def, skel, empty, flat);
    expect(culled.indices.length).toBe(0);
    expect(full.indices.length).toBeGreaterThan(1000);
    // every head vertex turns with the head, no tail vertex does
    const stride = 17;
    const range = (name: string) => full.ranges.find((r) => r.name === name)!;
    const headness = (name: string): number[] => {
      const r = range(name);
      const ids = new Set(Array.from(full.indices.slice(r.first, r.first + r.count)));
      return [...ids].map((i) => full.vertices[i * stride + 16]);
    };
    expect(Math.min(...headness('ear_l'))).toBeGreaterThan(0.99);
    expect(Math.max(...headness('tail_l'))).toBe(0);
  });
});
