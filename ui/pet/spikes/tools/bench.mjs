// Real-time cost of each prototype at the OBS size (520x520), headless Chromium with GPU.
// usage: node tools/bench.mjs <out.json> [seconds=15] [base=http://localhost:5198]
// Measures: frame rate and frame-time percentiles (rAF), JS time per frame, main-thread busy
// time (CDP Performance.TaskDuration) and CPU time of the renderer and GPU processes
// (CDP SystemInfo.getProcessInfo), plus GPU time per frame where the timer query exists.
import { writeFileSync } from 'node:fs';
import { chromium } from 'playwright';

const [out, secArg = '15', base = 'http://localhost:5198'] = process.argv.slice(2);
const seconds = Number(secArg);
const pages = ['rig2', 'threed', 'relief'];
const results = {};

for (const name of pages) {
  const browser = await chromium.launch({ headless: true, args: ['--use-angle=d3d11', '--ignore-gpu-blocklist', '--enable-gpu-rasterization'] });
  const ctx = await browser.newContext({ viewport: { width: 520, height: 520 } });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => console.error('[pageerror]', e.message));
  await page.goto(`${base}/${name}/?mode=bench&w=520&h=520`);
  await page.waitForFunction(() => window.__spike !== undefined, null, { timeout: 60000 });
  await page.evaluate(() => window.__spike.bench(2)); // warm-up: shader compile, JIT, texture upload

  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Performance.enable');
  const bcdp = await browser.newBrowserCDPSession();
  const procs = async () => (await bcdp.send('SystemInfo.getProcessInfo')).processInfo;
  const metric = (m, n) => m.metrics.find((x) => x.name === n)?.value ?? 0;

  const m0 = await cdp.send('Performance.getMetrics');
  const p0 = await procs();
  const t0 = Date.now();
  const r = await page.evaluate((s) => window.__spike.bench(s), seconds);
  const wall = (Date.now() - t0) / 1000;
  const m1 = await cdp.send('Performance.getMetrics');
  const p1 = await procs();

  const cpuBy = (type) => {
    const sum = (list) => list.filter((p) => p.type === type).reduce((a, p) => a + p.cpuTime, 0);
    return ((sum(p1) - sum(p0)) / wall) * 100;
  };
  results[name] = {
    ...r,
    wallSeconds: wall,
    mainThreadBusyPct: ((metric(m1, 'TaskDuration') - metric(m0, 'TaskDuration')) / wall) * 100,
    scriptPct: ((metric(m1, 'ScriptDuration') - metric(m0, 'ScriptDuration')) / wall) * 100,
    rendererCpuPct: cpuBy('renderer'),
    gpuProcessCpuPct: cpuBy('GPU'),
    browserCpuPct: cpuBy('browser'),
    jsHeapMB: metric(m1, 'JSHeapUsedSize') / 1048576,
  };
  console.log(name, JSON.stringify(results[name]));
  await browser.close();
  await new Promise((res) => setTimeout(res, 3000)); // let the machine settle between runs
}
writeFileSync(out, JSON.stringify(results, null, 2));
