/**
 * Small numeric helpers shared by the relief renderer. Pure, allocation-free where it matters
 * (the per-frame path), and in pixels of the creature's key art with y pointing down.
 */

export type Vec2 = [number, number];

/** 2D affine `[a, b, c, d, tx, ty]`: `x' = a x + c y + tx`, `y' = b x + d y + ty`. */
export type Affine = Float64Array;

export const identity = (): Affine => new Float64Array([1, 0, 0, 1, 0, 0]);

/** `out = m * n`. `out` may alias neither input. */
export function mulInto(out: Affine, m: Affine, n: Affine): Affine {
  const a = m[0] * n[0] + m[2] * n[1];
  const b = m[1] * n[0] + m[3] * n[1];
  const c = m[0] * n[2] + m[2] * n[3];
  const d = m[1] * n[2] + m[3] * n[3];
  const tx = m[0] * n[4] + m[2] * n[5] + m[4];
  const ty = m[1] * n[4] + m[3] * n[5] + m[5];
  out[0] = a;
  out[1] = b;
  out[2] = c;
  out[3] = d;
  out[4] = tx;
  out[5] = ty;
  return out;
}

export const applyX = (m: Affine, x: number, y: number): number => m[0] * x + m[2] * y + m[4];
export const applyY = (m: Affine, x: number, y: number): number => m[1] * x + m[3] * y + m[5];
export const apply = (m: Affine, x: number, y: number): Vec2 => [applyX(m, x, y), applyY(m, x, y)];

export const clamp = (x: number, lo = 0, hi = 1): number => Math.min(hi, Math.max(lo, x));
export const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;

/** Smoothstep of `x` between `a` and `b` (0 below a, 1 above b). */
export function ramp(x: number, a: number, b: number): number {
  const t = clamp((x - a) / (b - a));
  return t * t * (3 - 2 * t);
}

/** Distance from p to segment ab, and the parameter along it (0..1). */
export function segDist(px: number, py: number, a: Vec2, b: Vec2): [number, number] {
  const vx = b[0] - a[0];
  const vy = b[1] - a[1];
  const l2 = vx * vx + vy * vy || 1;
  const t = clamp(((px - a[0]) * vx + (py - a[1]) * vy) / l2);
  return [Math.hypot(px - (a[0] + t * vx), py - (a[1] + t * vy)), t];
}

/** Seeded PRNG in [0, 1): a preview with a fixed seed is reproducible frame for frame. */
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
 * Frame-rate independent exponential approach: the fraction of the remaining distance covered in
 * `dt` seconds with time constant `tau`.
 */
export const approach = (dt: number, tau: number): number => (tau <= 0 ? 1 : 1 - Math.exp(-dt / tau));

/** Row-major 3x3 rotation `Ry(yaw) * Rx(pitch)`; y is down, z towards the viewer. */
export function rotYX(yaw: number, pitch: number, out: Float32Array = new Float32Array(9)): Float32Array {
  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);
  out[0] = cy;
  out[1] = sy * sp;
  out[2] = sy * cp;
  out[3] = 0;
  out[4] = cp;
  out[5] = -sp;
  out[6] = -sy;
  out[7] = cy * sp;
  out[8] = cy * cp;
  return out;
}
