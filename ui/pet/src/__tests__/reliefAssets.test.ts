/**
 * The relief assets in the repository are exactly what `scripts/relief/build_relief.py` makes from
 * the checked-in configs, art and script: each creature's `relief/build.json` records the hashes of
 * all of them, and of the outputs. A config, script or picture changed without rebuilding - or an
 * output edited by hand - fails here. (CI additionally rebuilds the assets and compares the bytes;
 * see the "Relief assets reproducible" step.)
 *
 * It also holds every converted creature to the cut's quality bar: the parts, put back together at
 * rest, must reproduce the painting (PSNR >= 40 dB).
 */

import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { parseReliefDefinition } from '../relief/reliefFile';
import { parseRigDefinition } from '../rig/rigFile';

const PET = process.cwd();
const VARIANTS = join(PET, 'public', 'variants');
const MIN_PSNR = 40;

interface Manifest {
  generator: string;
  inputs: Record<string, string>;
  outputs: Record<string, string>;
  psnr: number;
}

function sha256(path: string): string {
  let data = readFileSync(path);
  if (path.endsWith('.json') || path.endsWith('.py')) {
    data = Buffer.from(data.toString('binary').replace(/\r\n/g, '\n'), 'binary');
  }
  return createHash('sha256').update(data).digest('hex');
}

const converted = readdirSync(VARIANTS).filter((v) => existsSync(join(VARIANTS, v, 'relief', 'build.json')));

it('finds converted creatures rather than trusting a list', () => {
  expect(converted.length).toBeGreaterThanOrEqual(7);
});

describe.each(converted)('relief assets of %s', (variant) => {
  const dir = join(VARIANTS, variant, 'relief');
  const manifest = JSON.parse(readFileSync(join(dir, 'build.json'), 'utf-8')) as Manifest;

  it('are the files the build recorded', () => {
    for (const [name, hash] of Object.entries(manifest.outputs)) expect(sha256(join(dir, name)), name).toBe(hash);
  });

  it('were built from the config, art and script as they are now', () => {
    for (const [path, hash] of Object.entries(manifest.inputs)) {
      expect(existsSync(join(PET, path)), path).toBe(true);
      expect(sha256(join(PET, path)), `${path} changed - rerun build_relief.py ${variant}`).toBe(hash);
    }
  });

  it('reproduce the painting at rest', () => {
    expect(manifest.psnr).toBeGreaterThanOrEqual(MIN_PSNR);
  });

  it('parse, and the creature switches to them', () => {
    const def = parseReliefDefinition(JSON.parse(readFileSync(join(dir, 'relief.json'), 'utf-8')), variant);
    expect(def.parts.length).toBeGreaterThanOrEqual(5);
    const rig = parseRigDefinition(JSON.parse(readFileSync(join(VARIANTS, variant, 'rig.json'), 'utf-8')), variant);
    expect(rig.relief).toEqual({ dir: 'relief', enabled: true });
  });
});
