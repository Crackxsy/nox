/**
 * Bundle budget for the pet window, run in CI after `npm run build`.
 *
 * The main chunk is what every pet loads; the relief renderer is a lazy chunk fetched only for a
 * creature that has been converted to it. Budgets are gzip -9 bytes with some headroom over what
 * ships today, so a real regression fails the build and ordinary edits do not.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';

const BUDGETS = { index: 100_000, ReliefPet: 16_000, reliefFile: 4_000 };

const dir = join(import.meta.dirname, '..', 'dist', 'assets');
const sizes = {};
for (const file of readdirSync(dir).filter((f) => f.endsWith('.js'))) {
  const name = file.split('-')[0];
  sizes[name] = (sizes[name] ?? 0) + gzipSync(readFileSync(join(dir, file)), { level: 9 }).length;
}

let failed = false;
for (const [name, budget] of Object.entries(BUDGETS)) {
  const size = sizes[name];
  if (size === undefined) {
    console.error(`bundle: chunk "${name}" is missing - was the lazy split lost?`);
    failed = true;
    continue;
  }
  const ok = size <= budget;
  console.log(`bundle: ${name.padEnd(12)} ${String(size).padStart(7)} B gzip (budget ${budget})${ok ? '' : '  OVER'}`);
  failed ||= !ok;
}
process.exit(failed ? 1 : 0);
