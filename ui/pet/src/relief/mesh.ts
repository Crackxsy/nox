/**
 * Geometry for the relief renderer: one grid mesh per painted part, skinned to up to four bones,
 * with a height (z) and a normal per vertex taken from the part's relief map.
 *
 * Built once when a creature loads; nothing here runs per frame. Every per-frame operation -
 * skinning, the 3D head/body turn, the perspective and the relighting - happens on the GPU from the
 * static attributes produced here (see `renderer.ts`).
 */

import { type Vec2, clamp, ramp, segDist } from './math';
import type { Skeleton } from './skeleton';
import type { ReliefDefinition, ReliefPart, WeightRule } from './reliefFile';

export type WeightFn = (x: number, y: number) => [string, number][];

const hat = (f: number, k: number, n: number): number => {
  if (k === 0 && f <= 0.5) return 1;
  if (k === n - 1 && f >= n - 0.5) return 1;
  return Math.max(0, 1 - Math.abs(f - (k + 0.5)));
};

/** Turn a declarative weight rule from the rig file into a weight function over art px. */
export function weightFn(rule: WeightRule): WeightFn {
  switch (rule.kind) {
    case 'rigid':
      return () => [[rule.bone, 1]];
    case 'chain': {
      const { points, bones, base, baseLen } = rule;
      return (x, y) => {
        let best = Infinity;
        let s = 0;
        for (let k = 0; k < points.length - 1; k++) {
          const [d, t] = segDist(x, y, points[k], points[k + 1]);
          if (d < best) {
            best = d;
            s = (k + t) / (points.length - 1);
          }
        }
        const wb = 1 - ramp(s, 0, baseLen);
        const f = s * bones.length;
        return [
          [base, wb],
          ...bones.map((b, k): [string, number] => [b, (1 - wb) * hat(f, k, bones.length)]),
        ];
      };
    }
    case 'ear': {
      const { base, tip, parent, bones } = rule;
      return (x, y) => {
        const [, s] = segDist(x, y, base, tip);
        const hw = 1 - ramp(s, -0.05, 0.14);
        const w2 = (1 - hw) * ramp(s, 0.35, 0.72);
        return [
          [parent, hw],
          [bones[0], 1 - hw - w2],
          [bones[1], w2],
        ];
      };
    }
    case 'spine': {
      const { bones, ys, legs } = rule;
      return (x, y) => {
        const out: [string, number][] = [];
        let legSum = 0;
        for (const leg of legs) {
          const w =
            (1 - ramp(Math.abs(x - leg.x), leg.halfWidth[0], leg.halfWidth[1])) * ramp(y, leg.y[0], leg.y[1]);
          legSum += w;
          out.push([leg.bone, w]);
        }
        const rest = Math.max(0, 1 - legSum);
        const n = bones.length;
        for (let k = 0; k < n; k++) {
          let w: number;
          if (y <= ys[k]) w = k === 0 ? 1 : clamp((y - ys[k - 1]) / (ys[k] - ys[k - 1]));
          else w = k === n - 1 ? 1 : clamp((ys[k + 1] - y) / (ys[k + 1] - ys[k]));
          out.push([bones[k], w * rest]);
        }
        return out;
      };
    }
    case 'head': {
      const { bone, neck, cheeks } = rule;
      return (x, y) => {
        const cw = cheeks
          ? ramp(Math.abs(x - cheeks.centerX), cheeks.dx[0], cheeks.dx[1]) * ramp(y, cheeks.y[0], cheeks.y[1])
          : 0;
        const nw = neck.weight * ramp(y, neck.y[0], neck.y[1]);
        const hw = Math.max(0, 1 - cw - nw);
        const out: [string, number][] = [
          [bone, hw],
          [neck.bone, nw],
        ];
        if (cheeks) out.push([x < cheeks.centerX ? cheeks.left : cheeks.right, cw]);
        return out;
      };
    }
  }
}

/** Pixels of one atlas (RGBA, row-major), as `getImageData` returns them. */
export interface Pixels {
  data: Uint8ClampedArray;
  width: number;
  height: number;
}

/** Height of a part's relief at art px (x, y), 0..1, from the depth atlas (red channel). */
export function sampleDepth(part: ReliefPart, depth: Pixels, x: number, y: number): number {
  const [rx, ry, rw, rh] = part.rect;
  const ix = Math.max(0, Math.min(rw - 1, Math.round(x - rx))) + part.atlas[0];
  const iy = Math.max(0, Math.min(rh - 1, Math.round(y - ry))) + part.atlas[1];
  return depth.data[(iy * depth.width + ix) * 4] / 255;
}

/** Interleaving of the static vertex buffer, in floats. */
export const VERTEX_FLOATS = 17;
/**
 * Offsets inside one vertex: rest position (2), atlas uv (2), bone indices (4), bone weights (4),
 * z + normal (4), "headness" (1, how much the head turn moves this vertex).
 */
export const OFFSETS = { rest: 0, uv: 2, bones: 4, weights: 8, zn: 12, head: 16 } as const;

export interface DrawRange {
  name: string;
  /** First index and index count in the shared index buffer. */
  first: number;
  count: number;
}

export interface EyeGeometry {
  name: string;
  /** Range in the eye index buffer. */
  first: number;
  count: number;
  /** Atlas rect of the open and the closed (lid skin) eye, normalised to the atlas size. */
  openUv: [number, number, number, number];
  closedUv: [number, number, number, number];
  ellipse: { c: Vec2; r: Vec2 };
  /** Eye size in art px: the iris offset is given in art px and divided by this. */
  size: Vec2;
}

export interface ReliefGeometry {
  vertices: Float32Array;
  indices: Uint32Array;
  /** One draw range per painted part in back-to-front order; `#eyes` marks where the eyes go. */
  ranges: DrawRange[];
  eyeVertices: Float32Array;
  eyeIndices: Uint32Array;
  eyes: EyeGeometry[];
  vertexCount: number;
}

interface GridOut {
  rest: number[];
  local: number[];
  idx: number[];
}

/** Grid over a w x h rect (art px), dropping cells that are fully transparent in the atlas. */
function grid(
  part: { rect: readonly number[]; atlas: readonly number[] },
  cell: number,
  atlas: Pixels | null,
): GridOut {
  const [rx, ry, rw, rh] = part.rect;
  const [ax, ay] = part.atlas;
  const cols = Math.max(1, Math.round(rw / cell));
  const rows = Math.max(1, Math.round(rh / cell));
  const keep = (cx: number, cy: number): boolean => {
    if (!atlas) return true;
    const x0 = Math.floor((cx / cols) * rw);
    const x1 = Math.ceil(((cx + 1) / cols) * rw);
    const y0 = Math.floor((cy / rows) * rh);
    const y1 = Math.ceil(((cy + 1) / rows) * rh);
    for (let y = Math.max(0, y0 - 1); y < Math.min(rh, y1 + 1); y++)
      for (let x = Math.max(0, x0 - 1); x < Math.min(rw, x1 + 1); x++)
        if (atlas.data[((ay + y) * atlas.width + ax + x) * 4 + 3] > 1) return true;
    return false;
  };
  const vid = new Int32Array((cols + 1) * (rows + 1)).fill(-1);
  const out: GridOut = { rest: [], local: [], idx: [] };
  const vert = (gx: number, gy: number): number => {
    const k = gy * (cols + 1) + gx;
    if (vid[k] < 0) {
      vid[k] = out.rest.length / 2;
      out.rest.push(rx + (gx / cols) * rw, ry + (gy / rows) * rh);
      out.local.push(gx / cols, gy / rows);
    }
    return vid[k];
  };
  for (let cy = 0; cy < rows; cy++)
    for (let cx = 0; cx < cols; cx++) {
      if (!keep(cx, cy)) continue;
      const a = vert(cx, cy);
      const b = vert(cx + 1, cy);
      const c = vert(cx, cy + 1);
      const d = vert(cx + 1, cy + 1);
      out.idx.push(a, b, c, b, d, c);
    }
  return out;
}

/** Up to four (bone index, weight) pairs, normalised, heaviest first. */
function topWeights(list: [string, number][], skel: Skeleton, where: string): [number[], number[]] {
  const kept = list
    .filter(([, w]) => w > 1e-4)
    .sort((p, q) => q[1] - p[1])
    .slice(0, 4);
  const sum = kept.reduce((s, [, w]) => s + w, 0) || 1;
  const bones = [0, 0, 0, 0];
  const weights = [0, 0, 0, 0];
  kept.forEach(([name, w], k) => {
    const bi = skel.index.get(name);
    if (bi === undefined) throw new Error(`${where}: weight to unknown bone ${name}`);
    bones[k] = bi;
    weights[k] = w / sum;
  });
  if (kept.length === 0) weights[0] = 1; // nothing claimed it: ride on the root
  return [bones, weights];
}

/**
 * Build the static geometry of a creature: every part's skinned grid with its relief, and the eye
 * decals that sit on the head's surface.
 */
export function buildGeometry(
  def: ReliefDefinition,
  skel: Skeleton,
  atlas: Pixels,
  depth: Pixels,
): ReliefGeometry {
  const partByName = new Map(def.parts.map((p) => [p.name, p]));
  const headBones = new Set(def.headBones);
  const neckIndex = skel.index.get(def.neck.bone) ?? -1;
  const verts: number[] = [];
  const idx: number[] = [];
  const ranges: DrawRange[] = [];
  const [aw, ah] = [atlas.width, atlas.height];

  const surfaceZ = (part: ReliefPart, x: number, y: number): number =>
    part.z + part.relief * sampleDepth(part, depth, x, y);
  const headness = (bones: number[], weights: number[]): number => {
    let h = 0;
    for (let k = 0; k < 4; k++) {
      const name = skel.bones[bones[k]].name;
      if (headBones.has(name)) h += weights[k];
      else if (bones[k] === neckIndex) h += def.neck.headness * weights[k];
    }
    return h;
  };

  for (const name of def.order) {
    if (name === '#eyes') {
      ranges.push({ name, first: idx.length, count: 0 });
      continue;
    }
    const part = partByName.get(name)!;
    const g = grid(part, part.cell, atlas);
    const base = verts.length / VERTEX_FLOATS;
    const fn = weightFn(part.weights);
    const surface = part.surface ? partByName.get(part.surface)! : null;
    for (let v = 0; v < g.rest.length / 2; v++) {
      const x = g.rest[v * 2];
      const y = g.rest[v * 2 + 1];
      const [bones, weights] = topWeights(fn(x, y), skel, `parts.${name}`);
      const z = surface ? surfaceZ(surface, x, y) + 1 : surfaceZ(part, x, y);
      const r = part.relief;
      const dx = (r * (sampleDepth(part, depth, x + 3, y) - sampleDepth(part, depth, x - 3, y))) / 6;
      const dy = (r * (sampleDepth(part, depth, x, y + 3) - sampleDepth(part, depth, x, y - 3))) / 6;
      const l = Math.hypot(dx, dy, 1);
      const u = (part.atlas[0] + g.local[v * 2] * part.rect[2]) / aw;
      const w = (part.atlas[1] + g.local[v * 2 + 1] * part.rect[3]) / ah;
      verts.push(x, y, u, w, ...bones, ...weights, z, -dx / l, -dy / l, 1 / l, headness(bones, weights));
    }
    ranges.push({ name, first: idx.length, count: g.idx.length });
    for (const i of g.idx) idx.push(base + i);
  }

  // Eyes are decals: a small grid glued to the head's relief, so the patch stays on the face when
  // the head turns. Their uv is the eye's own 0..1 square (the eye shader needs it for the lids).
  const eyeVerts: number[] = [];
  const eyeIdx: number[] = [];
  const eyes: EyeGeometry[] = [];
  for (const eye of def.eyes) {
    const surface = partByName.get(eye.surface)!;
    const g = grid({ rect: eye.rect, atlas: eye.atlas }, 8, null);
    const base = eyeVerts.length / VERTEX_FLOATS;
    const [bones, weights] = topWeights([[eye.bone, 1]], skel, `eyes.${eye.name}`);
    for (let v = 0; v < g.rest.length / 2; v++) {
      const x = g.rest[v * 2];
      const y = g.rest[v * 2 + 1];
      const z = surfaceZ(surface, x, y) + 0.5;
      eyeVerts.push(x, y, g.local[v * 2], g.local[v * 2 + 1], ...bones, ...weights, z, 0, 0, 1, 1);
    }
    eyes.push({
      name: eye.name,
      first: eyeIdx.length,
      count: g.idx.length,
      openUv: [eye.atlas[0] / aw, eye.atlas[1] / ah, eye.rect[2] / aw, eye.rect[3] / ah],
      closedUv: [eye.closedAtlas[0] / aw, eye.closedAtlas[1] / ah, eye.rect[2] / aw, eye.rect[3] / ah],
      ellipse: eye.ellipse,
      size: [eye.rect[2], eye.rect[3]],
    });
    for (const i of g.idx) eyeIdx.push(base + i);
  }

  return {
    vertices: new Float32Array(verts),
    indices: new Uint32Array(idx),
    ranges,
    eyeVertices: new Float32Array(eyeVerts),
    eyeIndices: new Uint32Array(eyeIdx),
    eyes,
    vertexCount: verts.length / VERTEX_FLOATS + eyeVerts.length / VERTEX_FLOATS,
  };
}

/**
 * CPU reference of the vertex shader's skinning: where a rest vertex lands under the current
 * world matrices. Used by tests and by the renderer for anchor points (shadow, head centre).
 */
export function skinPoint(
  skel: Skeleton,
  bones: ArrayLike<number>,
  weights: ArrayLike<number>,
  x: number,
  y: number,
): Vec2 {
  let ox = 0;
  let oy = 0;
  for (let k = 0; k < 4; k++) {
    const w = weights[k];
    if (w === 0) continue;
    const m = skel.world[bones[k]];
    ox += w * (m[0] * x + m[2] * y + m[4]);
    oy += w * (m[1] * x + m[3] * y + m[5]);
  }
  return [ox, oy];
}
