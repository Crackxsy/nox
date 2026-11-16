// Bundle size per prototype from the Vite build (JS that the page loads + its runtime assets).
// usage: node tools/sizes.mjs <out.json>
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';

const dist = new URL('../dist/', import.meta.url).pathname.replace(/^\/(\w:)/, '$1');
const assets = {
  rig2: ['eulenfuchs/parts.json', ...readdirSync(join(dist, 'eulenfuchs')).filter((f) => f.endsWith('.png') && !f.includes('_depth')).map((f) => `eulenfuchs/${f}`), 'envelope.json'],
  relief: ['eulenfuchs/parts.json', ...readdirSync(join(dist, 'eulenfuchs')).filter((f) => f.endsWith('.png')).map((f) => `eulenfuchs/${f}`), 'envelope.json'],
  threed: ['fox/Fox.glb', 'envelope.json'],
};
const out = {};
for (const page of Object.keys(assets)) {
  const html = readFileSync(join(dist, page, 'index.html'), 'utf8');
  const js = new Set();
  const walk = (file) => {
    if (js.has(file)) return;
    js.add(file);
    const src = readFileSync(join(dist, file), 'utf8');
    for (const m of src.matchAll(/(?:from|import)\s*\(?\s*["'](?:\.\/|\/assets\/)?([\w.-]+\.js)["']/g)) walk(`assets/${m[1]}`);
  };
  for (const m of html.matchAll(/(?:src|href)="\/?(assets\/[\w.-]+\.js)"/g)) walk(m[1]);
  let raw = 0;
  let gz = 0;
  for (const f of js) {
    const b = readFileSync(join(dist, f));
    raw += b.length;
    gz += gzipSync(b).length;
  }
  const assetBytes = assets[page].reduce((a, f) => a + statSync(join(dist, f)).size, 0);
  out[page] = { jsFiles: [...js], jsKB: +(raw / 1024).toFixed(1), jsGzipKB: +(gz / 1024).toFixed(1), assetsKB: +(assetBytes / 1024).toFixed(1) };
}
console.log(JSON.stringify(out, null, 2));
writeFileSync(process.argv[2], JSON.stringify(out, null, 2));
