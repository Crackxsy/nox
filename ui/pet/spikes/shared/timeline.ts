/**
 * The one script all three prototypes perform, so the videos compare like with like.
 * Pure functions of time (plus a seeded RNG) so a recording is reproducible frame by frame.
 */

export const DURATION = 18.5;

export interface Phase {
  id: 'idle' | 'look' | 'hop' | 'sleep' | 'wake' | 'talk';
  t0: number;
  t1: number;
  label: string;
}

export const PHASES: Phase[] = [
  { id: 'idle', t0: 0, t1: 3, label: 'Idle – Atmen, Blinzeln, Ohren & Schwanz' },
  { id: 'look', t0: 3, t1: 6.5, label: 'Blick folgt dem Punkt' },
  { id: 'hop', t0: 6.5, t1: 9.5, label: 'Laufen / Hüpfen' },
  { id: 'sleep', t0: 9.5, t1: 13, label: 'Hinsetzen → Einschlafen' },
  { id: 'wake', t0: 13, t1: 15, label: 'Aufwachen + Freude' },
  { id: 'talk', t0: 15, t1: DURATION, label: 'Sprechen – Kiefer folgt der Lautstärke' },
];

export const TALK_START = 15.05;

/** Screen layout shared by all prototypes (fractions of the canvas). */
export const LAYOUT = {
  groundV: 0.9,
  /** Creature height as a fraction of the canvas height (limited by width for square canvases). */
  heightOf(w: number, h: number): number {
    return Math.min(h * 0.74, w * 0.4);
  },
};

export function phaseAt(t: number): Phase {
  return PHASES.find((p) => t >= p.t0 && t < p.t1) ?? PHASES[PHASES.length - 1];
}

export const clamp = (x: number, a = 0, b = 1): number => Math.min(b, Math.max(a, x));
export const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;
export function smooth(a: number, b: number, t: number): number {
  const x = clamp((t - a) / (b - a));
  return x * x * (3 - 2 * x);
}
/** 0 before a, rises to 1 at b, stays until c, falls to 0 at d. */
export function window(t: number, a: number, b: number, c: number, d: number): number {
  return smooth(a, b, t) * (1 - smooth(c, d, t));
}

export function mulberry32(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let r = Math.imul(s ^ (s >>> 15), 1 | s);
    r = (r + Math.imul(r ^ (r >>> 7), 61 | r)) ^ r;
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

/** Smooth 1D value noise in [-1, 1]. */
export function noise1(x: number, seed = 0): number {
  const hash = (i: number): number => {
    const v = Math.sin(i * 127.1 + seed * 311.7) * 43758.5453;
    return (v - Math.floor(v)) * 2 - 1;
  };
  const i = Math.floor(x);
  const f = x - i;
  const u = f * f * (3 - 2 * f);
  return lerp(hash(i), hash(i + 1), u);
}

/**
 * The point the creature looks at during the "look" phase, in canvas fractions (u right, v down).
 * Outside that phase `w` is 0 and the creature looks at the viewer.
 */
export function lookTarget(t: number): { u: number; v: number; w: number } {
  const p = PHASES[1];
  const w = window(t, p.t0, p.t0 + 0.35, p.t1 - 0.4, p.t1);
  const tau = t - p.t0;
  const u = 0.5 + 0.3 * Math.sin(2 * Math.PI * 0.27 * tau + 0.2);
  const v = 0.34 + 0.2 * Math.sin(2 * Math.PI * 0.43 * tau + 1.9);
  return { u, v, w };
}

/**
 * Hop plan: four hops, two to the right and two back. `x` is the travel in creature heights,
 * `lift` the height of the jump in creature heights, `squash` < 0 squash / > 0 stretch, `air` 0..1.
 */
export interface HopState {
  x: number;
  lift: number;
  squash: number;
  air: number;
  dir: number;
  active: number;
}

const HOPS = [
  { t: 6.85, from: 0, to: 0.2 },
  { t: 7.6, from: 0.2, to: 0.4 },
  { t: 8.45, from: 0.4, to: 0.2 },
  { t: 9.15, from: 0.2, to: 0.0 },
];
const AIR = 0.38;
const ANTICIPATE = 0.16;
const LAND = 0.2;

export function hopPlan(t: number): HopState {
  let x = 0;
  let lift = 0;
  let squash = 0;
  let air = 0;
  let dir = 1;
  for (const h of HOPS) {
    if (t >= h.t + AIR) x = h.to;
    if (t >= h.t - ANTICIPATE && t < h.t + AIR + LAND) dir = Math.sign(h.to - h.from);
    if (t < h.t - ANTICIPATE || t > h.t + AIR + LAND) continue;
    if (t < h.t) {
      const k = (t - (h.t - ANTICIPATE)) / ANTICIPATE;
      squash = -Math.sin(k * Math.PI * 0.5);
      x = h.from;
    } else if (t < h.t + AIR) {
      const k = (t - h.t) / AIR;
      air = Math.sin(k * Math.PI);
      lift = 0.13 * 4 * k * (1 - k);
      const e = k * k * (3 - 2 * k);
      x = lerp(h.from, h.to, e);
      squash = k < 0.5 ? 0.9 * (1 - k * 2) : 0.45 * (k * 2 - 1); // stretch at take-off and falling
    } else {
      const k = (t - h.t - AIR) / LAND;
      squash = -Math.sin(Math.min(1, k * 1.6) * Math.PI) * (1 - k) * 1.1;
      x = h.to;
    }
  }
  const active = window(t, 6.5, 6.7, 9.6, 9.8);
  return { x, lift, squash, air, dir, active };
}

export interface Envelope {
  /** Raw RMS at time s (0..1) since speech start. */
  at(s: number): number;
  duration: number;
}

export async function loadEnvelope(url: string): Promise<Envelope> {
  const data = (await (await fetch(url)).json()) as { fps: number; duration: number; values: number[] };
  const { fps, values } = data;
  return {
    duration: data.duration,
    at(s: number) {
      if (s < 0) return 0;
      const f = s * fps;
      const i = Math.floor(f);
      if (i >= values.length - 1) return 0;
      return lerp(values[i], values[i + 1], f - i);
    },
  };
}
