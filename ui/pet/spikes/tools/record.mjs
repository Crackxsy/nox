// Record a prototype headlessly, frame by frame, into an MP4 (H.264 + the babble audio track).
// usage: node tools/record.mjs <page> <out.mp4> [fps=60] [w=1280] [h=720] [base=http://localhost:5198]
// needs FFMPEG=<path to ffmpeg> (e.g. from `uv run --with imageio-ffmpeg`), no window is opened.
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const [page, out, fpsArg = '60', wArg = '1280', hArg = '720', base = 'http://localhost:5198'] = process.argv.slice(2);
const fps = Number(fpsArg);
const w = Number(wArg);
const h = Number(hArg);
const ffmpeg = process.env.FFMPEG;
if (!ffmpeg) throw new Error('set FFMPEG');
const wav = fileURLToPath(new URL('../public/babble.wav', import.meta.url));

const browser = await chromium.launch({ headless: true, args: ['--use-angle=d3d11', '--ignore-gpu-blocklist', '--enable-gpu-rasterization'] });
const p = await browser.newPage({ viewport: { width: w, height: h } });
p.on('pageerror', (e) => console.error('[pageerror]', e.message));
await p.goto(`${base}/${page}/?mode=record&w=${w}&h=${h}`);
await p.waitForFunction(() => window.__spike !== undefined, null, { timeout: 60000 });
const { duration, talkStart } = await p.evaluate(() => ({ duration: window.__spike.duration, talkStart: window.__spike.talkStart }));

const delayMs = Math.round(talkStart * 1000);
const ff = spawn(ffmpeg, [
  '-y', '-loglevel', 'error',
  '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'mjpeg', '-i', '-',
  '-i', wav,
  '-filter_complex', `[1:a]adelay=${delayMs}|${delayMs},apad[a]`,
  '-map', '0:v', '-map', '[a]', '-shortest',
  '-c:v', 'libx264', '-preset', 'slow', '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
  '-c:a', 'aac', '-b:a', '128k',
  out,
], { stdio: ['pipe', 'inherit', 'inherit'] });

const frames = Math.round(duration * fps);
const started = Date.now();
for (let i = 0; i <= frames; i++) {
  const t = Math.min(duration - 1e-3, i / fps);
  await p.evaluate((x) => window.__spike.renderAt(x), t);
  const jpg = await p.screenshot({ type: 'jpeg', quality: 95 });
  if (!ff.stdin.write(jpg)) await new Promise((r) => ff.stdin.once('drain', r));
  if (i % 120 === 0) console.log(`${page}: frame ${i}/${frames}`);
}
ff.stdin.end();
await new Promise((r) => ff.on('close', r));
await browser.close();
console.log(`${page}: ${frames + 1} frames in ${((Date.now() - started) / 1000).toFixed(0)} s -> ${out}`);
