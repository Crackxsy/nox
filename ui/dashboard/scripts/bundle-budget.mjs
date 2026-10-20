/**
 * Performance budget for the dashboard build (Spec UX-Neubau §12), run after `vite build`:
 *
 *   start JS (the entry chunk and everything it imports statically), gzip   <= 110 KB
 *   each lazily loaded area (its chunk plus what only it pulls in), gzip    <=  35 KB
 *   all CSS, gzip                                                           <=  12 KB
 *
 * The chunk graph comes from Vite's manifest (`dist/.vite/manifest.json`). A lazy chunk is charged
 * for its own file and for every statically imported chunk that is not already part of the start
 * bundle - that is what a first visit to the area downloads. Two corrections make the numbers what a
 * reader actually downloads: the start dictionary (`de` / `en`, fetched before the first render) is
 * part of the start bundle - the larger of the two is counted - and an area's first visit also
 * fetches the area copy (`de.more` / `en.more`), the larger of which is added to every area.
 * Exits non-zero over budget; prints the table either way, so CI logs show the trend.
 *
 * Usage: node scripts/bundle-budget.mjs [distDir]
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';

const KB = 1024;
export const BUDGET = { startJs: 110 * KB, lazyChunk: 35 * KB, css: 12 * KB };

const dist = process.argv[2] ?? 'dist';
const manifest = JSON.parse(readFileSync(join(dist, '.vite', 'manifest.json'), 'utf8'));

const gz = new Map();
function gzipped(file) {
  if (!gz.has(file)) gz.set(file, gzipSync(readFileSync(join(dist, file)), { level: 9 }).length);
  return gz.get(file);
}

/** Every chunk key reachable from `key` through static imports, `key` included. */
function staticClosure(key, seen = new Set()) {
  if (seen.has(key)) return seen;
  seen.add(key);
  for (const next of manifest[key]?.imports ?? []) staticClosure(next, seen);
  return seen;
}

const entryKey = Object.keys(manifest).find((k) => manifest[k].isEntry);
if (!entryKey) throw new Error('no entry chunk in the manifest');
const start = staticClosure(entryKey);
const bySourceName = (name) => Object.keys(manifest).filter((k) => manifest[k].name === name);
const largest = (names) =>
  Math.max(0, ...names.flatMap(bySourceName).map((k) => gzipped(manifest[k].file)));
const DICTS = ['de', 'en'];
const AREA_COPY = ['de.more', 'en.more'];
const startDict = largest(DICTS);
const areaCopy = largest(AREA_COPY);
const startJs = [...start].reduce((sum, k) => sum + gzipped(manifest[k].file), 0) + startDict;

const css = new Set();
for (const chunk of Object.values(manifest)) for (const file of chunk.css ?? []) css.add(file);
const cssTotal = [...css].reduce((sum, f) => sum + gzipped(f), 0);

const lazyKeys = new Set();
for (const k of Object.keys(manifest)) for (const d of manifest[k].dynamicImports ?? []) lazyKeys.add(d);
const lazy = [...lazyKeys]
  .filter((k) => ![...DICTS, ...AREA_COPY].includes(manifest[k].name))
  .map((k) => {
    const own = [...staticClosure(k)].filter((c) => !start.has(c));
    const size = own.reduce((sum, c) => sum + gzipped(manifest[c].file), 0);
    return { name: manifest[k].name ?? k, size: size + areaCopy };
  })
  .sort((a, b) => b.size - a.size);

const rows = [
  ['start JS (shell + Heute + start dictionary)', startJs, BUDGET.startJs],
  ['CSS (all)', cssTotal, BUDGET.css],
  ...lazy.map((l) => [`lazy: ${l.name} (+ area copy)`, l.size, BUDGET.lazyChunk]),
];
let failed = false;
for (const [name, size, budget] of rows) {
  const over = size > budget;
  failed ||= over;
  console.log(`${over ? 'OVER' : 'ok  '}  ${(size / KB).toFixed(1).padStart(6)} KB / ${(budget / KB).toFixed(0)} KB  ${name}`);
}
if (failed) {
  console.error('bundle budget exceeded (Spec UX-Neubau §12)');
  process.exit(1);
}
