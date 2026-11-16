// Headless snapshots of a prototype at given script times (development aid, no visible window).
// usage: node tools/snap.mjs <page> <out-prefix> t1 t2 ...   (dev server must run on :5199)
import { chromium } from 'playwright';

const [page, out, ...times] = process.argv.slice(2);
const browser = await chromium.launch({ headless: true, args: ['--use-angle=d3d11', '--ignore-gpu-blocklist'] });
const p = await browser.newPage({ viewport: { width: 1280, height: 720 } });
p.on('console', (m) => console.log('[page]', m.type(), m.text()));
p.on('pageerror', (e) => console.log('[pageerror]', e.message));
await p.goto(`http://localhost:5199/${page}/?mode=record&w=1280&h=720`);
await p.waitForFunction(() => window.__spike !== undefined, null, { timeout: 30000 });
let last = 0;
for (const ts of times) {
  const t = Number(ts);
  // run the simulation up to t at 60 fps so springs/state are right
  await p.evaluate(([a, b]) => { for (let x = a; x < b; x += 1 / 60) window.__spike.renderAt(x); window.__spike.renderAt(b); }, [last, t]);
  last = t;
  await p.screenshot({ path: `${out}_${ts}.png` });
}
await browser.close();
