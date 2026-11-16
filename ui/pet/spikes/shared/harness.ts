/**
 * Shared page harness: canvas setup, caption overlay, deterministic recording hook and a
 * real-time benchmark loop. URL parameters:
 *   mode=live|record|bench   (live: loops in real time; record: driven frame by frame; bench: live, no overlay)
 *   w, h                     canvas size in CSS px (device pixel ratio is forced to 1 for comparability)
 */
import { DURATION, LAYOUT, PHASES, TALK_START, loadEnvelope, lookTarget, phaseAt, type Envelope } from './timeline';

export interface SpikeContext {
  w: number;
  h: number;
  /** Creature height in px. */
  H: number;
  groundY: number;
  centerX: number;
  envelope: Envelope;
}

export interface Spike {
  title: string;
  subtitle: string;
  init(canvas: HTMLCanvasElement, ctx: SpikeContext): Promise<void>;
  /** Advance to script time t (s); dt is the step since the previous call, 0 after reset. */
  frame(t: number, dt: number): void;
  reset(): void;
  gl?: WebGL2RenderingContext;
}

interface BenchResult {
  seconds: number;
  frames: number;
  fps: number;
  frameMsP50: number;
  frameMsP95: number;
  jsMsAvg: number;
  jsMsP95: number;
  gpuMsAvg: number | null;
  renderer: string;
}

declare global {
  interface Window {
    __spike?: {
      ready: Promise<void>;
      renderAt(t: number): void;
      bench(seconds: number): Promise<BenchResult>;
      duration: number;
      talkStart: number;
    };
  }
}

export async function boot(spike: Spike): Promise<void> {
  const q = new URLSearchParams(location.search);
  const mode = q.get('mode') ?? 'live';
  const w = Number(q.get('w') ?? 1280);
  const h = Number(q.get('h') ?? 720);

  document.title = spike.title;
  const style = document.createElement('style');
  style.textContent = `
    html,body{margin:0;background:#26272b;overflow:hidden}
    #stage{position:relative;width:${w}px;height:${h}px}
    canvas{position:absolute;inset:0;width:${w}px;height:${h}px}
    .ov{position:absolute;font:500 ${Math.round(h / 40)}px/1.3 "Segoe UI",system-ui,sans-serif;color:#e8e6e3;
        text-shadow:0 1px 2px #0008;pointer-events:none}
    #title{left:${h / 36}px;top:${h / 40}px}
    #title small{display:block;font-weight:400;opacity:.65;font-size:.78em}
    #phase{left:${h / 36}px;bottom:${h / 40}px;opacity:.9}
    #time{right:${h / 36}px;top:${h / 40}px;opacity:.5;font-variant-numeric:tabular-nums}
    #meter{right:${h / 36}px;bottom:${h / 40}px;width:${h / 4}px;height:${h / 90}px;border-radius:4px;
           background:#ffffff1a;overflow:hidden;opacity:0;transition:none}
    #meter i{display:block;height:100%;width:0;background:#e9a35b}
    #meterLabel{right:${h / 36}px;bottom:${h / 40 + h / 50}px;font-size:${h / 60}px;opacity:0}
    #dot{position:absolute;width:${h / 45}px;height:${h / 45}px;margin:-${h / 90}px 0 0 -${h / 90}px;border-radius:50%;
         background:radial-gradient(circle,#fff 0 35%,#ffd27a 45%,#ffd27a00 72%);opacity:0;pointer-events:none}
  `;
  document.head.appendChild(style);

  const stage = document.createElement('div');
  stage.id = 'stage';
  document.body.appendChild(stage);
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  stage.appendChild(canvas);

  const overlay = mode !== 'bench';
  const el = (id: string, html = ''): HTMLElement => {
    const d = document.createElement('div');
    d.id = id;
    d.className = 'ov';
    d.innerHTML = html;
    if (overlay) stage.appendChild(d);
    return d;
  };
  el('title', `${spike.title}<small>${spike.subtitle}</small>`);
  const phaseEl = el('phase');
  const timeEl = el('time');
  const meter = el('meter', '<i></i>');
  const meterLabel = el('meterLabel', 'Lautstärke (Audio)');
  const dot = document.createElement('div');
  dot.id = 'dot';
  if (overlay) stage.appendChild(dot);

  const envelope = await loadEnvelope('/envelope.json');
  const H = LAYOUT.heightOf(w, h);
  const ctx: SpikeContext = { w, h, H, groundY: h * LAYOUT.groundV, centerX: w / 2, envelope };
  await spike.init(canvas, ctx);

  const updateOverlay = (t: number): void => {
    if (!overlay) return;
    phaseEl.textContent = `${PHASES.indexOf(phaseAt(t)) + 1}/6 · ${phaseAt(t).label}`;
    timeEl.textContent = `${t.toFixed(1)} s`;
    const lt = lookTarget(t);
    dot.style.opacity = String(lt.w);
    dot.style.left = `${lt.u * w}px`;
    dot.style.top = `${lt.v * h}px`;
    const talk = t >= TALK_START - 0.2 && t < DURATION ? 1 : 0;
    meter.style.opacity = meterLabel.style.opacity = String(talk * 0.9);
    (meter.firstChild as HTMLElement).style.width = `${envelope.at(t - TALK_START) * 100}%`;
  };

  // GPU time per frame, where the browser exposes the timer query (often it does not).
  const gl = spike.gl;
  const timerExt = gl?.getExtension('EXT_disjoint_timer_query_webgl2') as
    | { TIME_ELAPSED_EXT: number; GPU_DISJOINT_EXT: number }
    | null
    | undefined;
  const pending: WebGLQuery[] = [];
  const gpuMs: number[] = [];
  const pollQueries = (): void => {
    if (!gl || !timerExt) return;
    while (pending.length) {
      const qq = pending[0];
      if (!gl.getQueryParameter(qq, gl.QUERY_RESULT_AVAILABLE)) break;
      if (!gl.getParameter(timerExt.GPU_DISJOINT_EXT)) gpuMs.push(gl.getQueryParameter(qq, gl.QUERY_RESULT) / 1e6);
      gl.deleteQuery(qq);
      pending.shift();
    }
  };

  let lastT = -1;
  const step = (t: number): number => {
    const dt = lastT < 0 || t < lastT ? 0 : Math.min(0.1, t - lastT);
    if (dt === 0) spike.reset();
    lastT = t;
    const s = performance.now();
    let qq: WebGLQuery | null = null;
    if (gl && timerExt) {
      qq = gl.createQuery();
      if (qq) gl.beginQuery(timerExt.TIME_ELAPSED_EXT, qq);
    }
    spike.frame(t, dt);
    if (gl && timerExt && qq) {
      gl.endQuery(timerExt.TIME_ELAPSED_EXT);
      pending.push(qq);
    }
    updateOverlay(t);
    return performance.now() - s;
  };

  let rendererName = 'unknown';
  if (gl) {
    const info = gl.getExtension('WEBGL_debug_renderer_info');
    rendererName = info ? String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL)) : String(gl.getParameter(gl.RENDERER));
  }

  const bench = (seconds: number): Promise<BenchResult> =>
    new Promise((resolve) => {
      const frameMs: number[] = [];
      const jsMs: number[] = [];
      let start = -1;
      let prev = -1;
      const loop = (now: number): void => {
        if (start < 0) start = now;
        const elapsed = (now - start) / 1000;
        if (prev >= 0) frameMs.push(now - prev);
        prev = now;
        jsMs.push(step(elapsed % DURATION));
        pollQueries();
        if (elapsed < seconds) requestAnimationFrame(loop);
        else {
          const pct = (a: number[], p: number): number => [...a].sort((x, y) => x - y)[Math.floor(a.length * p)] ?? 0;
          const avg = (a: number[]): number => a.reduce((x, y) => x + y, 0) / Math.max(1, a.length);
          resolve({
            seconds: elapsed,
            frames: frameMs.length,
            fps: frameMs.length / elapsed,
            frameMsP50: pct(frameMs, 0.5),
            frameMsP95: pct(frameMs, 0.95),
            jsMsAvg: avg(jsMs),
            jsMsP95: pct(jsMs, 0.95),
            gpuMsAvg: gpuMs.length ? avg(gpuMs) : null,
            renderer: rendererName,
          });
        }
      };
      requestAnimationFrame(loop);
    });

  window.__spike = {
    ready: Promise.resolve(),
    renderAt: (t: number) => {
      step(t);
    },
    bench,
    duration: DURATION,
    talkStart: TALK_START,
  };

  if (mode === 'live') {
    let t0 = -1;
    const loop = (now: number): void => {
      if (t0 < 0) t0 = now;
      step(((now - t0) / 1000) % DURATION);
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }
}
