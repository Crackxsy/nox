/**
 * Print the bone weights the Meereswolf rig gives to a handful of landmarks.
 *
 * Authoring aid, not a test. Influence radii are the one part of `rig.json` whose effect cannot be
 * seen by looking at the picture: a pivot in the right place with too small a radius produces a
 * creature that is anchored to a motionless root and barely moves at all. This prints the number
 * that decides it.
 *
 *     node ui/pet/scripts/weights_probe.mjs
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const rig = JSON.parse(
  readFileSync(join(here, '..', 'public', 'variants', 'meereswolf', 'rig.json'), 'utf8'),
);

const LANDMARKS = {
  'eye.l': [0.532, 0.25],
  nose: [0.601, 0.356],
  'ear.l tip': [0.434, 0.03],
  'fin.l blade': [0.39, 0.26],
  chest: [0.585, 0.6],
  belly: [0.575, 0.8],
  'tail tip': [0.18, 0.86],
  'front paw': [0.6, 0.95],
};

function influenceAt(distance, radius, falloff) {
  if (radius <= 0 || distance >= radius) return 0;
  const near = 1 - distance / radius;
  return Math.pow(near * near * (3 - 2 * near), falloff);
}

for (const [label, [x, y]] of Object.entries(LANDMARKS)) {
  const weights = rig.bones.map((bone) =>
    bone.channelOnly
      ? 0
      : influenceAt(
          Math.hypot(x - bone.pivot[0], y - bone.pivot[1]),
          bone.influence.radius,
          bone.influence.falloff ?? 1,
        ),
  );
  const total = weights.reduce((sum, w) => sum + w, 0);
  for (let i = 0; i < weights.length; i += 1) weights[i] = total > 1e-4 ? weights[i] / total : 0;
  const shown = rig.bones
    .map((bone, i) => [bone.name, weights[i]])
    .filter(([, w]) => w > 0.02)
    .sort((a, b) => b[1] - a[1])
    .map(([name, w]) => `${name} ${w.toFixed(2)}`)
    .join('  ');
  console.log(`${label.padEnd(12)} ${shown}`);
}
