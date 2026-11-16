/**
 * Minimal 2D skeletal rig: bone hierarchy with pivot-based local transforms, linear-blend
 * skinning of per-part grid meshes, and "jiggle" bones (damped point mass that the bone aims at)
 * for secondary motion. Everything is in pixels of the 512 px key art, y down.
 */

export type Vec2 = [number, number];
/** 2D affine [a, b, c, d, tx, ty]: x' = a x + c y + tx, y' = b x + d y + ty. */
export type Mat = Float64Array;

export const identity = (): Mat => new Float64Array([1, 0, 0, 1, 0, 0]);

export function mul(m: Mat, n: Mat, out: Mat = new Float64Array(6)): Mat {
  const a = m[0] * n[0] + m[2] * n[1];
  const b = m[1] * n[0] + m[3] * n[1];
  const c = m[0] * n[2] + m[2] * n[3];
  const d = m[1] * n[2] + m[3] * n[3];
  const tx = m[0] * n[4] + m[2] * n[5] + m[4];
  const ty = m[1] * n[4] + m[3] * n[5] + m[5];
  out[0] = a; out[1] = b; out[2] = c; out[3] = d; out[4] = tx; out[5] = ty;
  return out;
}

export const apply = (m: Mat, x: number, y: number): Vec2 => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];

export interface BoneDef {
  name: string;
  parent: string | null;
  pivot: Vec2;
  /** End of the bone (for weights and jiggle length). */
  tip: Vec2;
  jiggle?: { stiffness: number; damping: number; gravity?: number; limit: number };
}

export interface BonePose {
  x: number;
  y: number;
  rot: number;
  sx: number;
  sy: number;
}

export const restPose = (): BonePose => ({ x: 0, y: 0, rot: 0, sx: 1, sy: 1 });

interface JiggleState {
  p: Vec2;
  v: Vec2;
  prevTarget: Vec2 | null;
  offset: number;
}

export class Skeleton {
  readonly bones: BoneDef[];
  readonly index = new Map<string, number>();
  readonly pose: BonePose[];
  readonly world: Mat[];
  private readonly parentIdx: number[];
  private jig: (JiggleState | null)[];
  /** Placement of the whole rig on screen (applied after the bone hierarchy). */
  placement: Mat = identity();

  constructor(defs: BoneDef[]) {
    this.bones = defs;
    defs.forEach((b, i) => this.index.set(b.name, i));
    this.parentIdx = defs.map((b) => (b.parent ? this.index.get(b.parent)! : -1));
    this.parentIdx.forEach((p, i) => {
      if (p >= i) throw new Error(`bone ${defs[i].name} must come after its parent`);
    });
    this.pose = defs.map(restPose);
    this.world = defs.map(identity);
    this.jig = defs.map(() => null);
  }

  get(name: string): BonePose {
    const i = this.index.get(name);
    if (i === undefined) throw new Error(`no bone ${name}`);
    return this.pose[i];
  }

  resetPose(): void {
    for (const p of this.pose) Object.assign(p, restPose());
  }

  resetPhysics(): void {
    this.jig = this.bones.map(() => null);
  }

  private local(i: number, extraRot: number): Mat {
    const b = this.bones[i];
    const p = this.pose[i];
    const [px, py] = b.pivot;
    const c = Math.cos(p.rot + extraRot);
    const s = Math.sin(p.rot + extraRot);
    // T(pivot + offset) * R * S * T(-pivot)
    const a = c * p.sx;
    const bb = s * p.sx;
    const cc = -s * p.sy;
    const d = c * p.sy;
    const tx = px + p.x - (a * px + cc * py);
    const ty = py + p.y - (bb * px + d * py);
    return new Float64Array([a, bb, cc, d, tx, ty]);
  }

  /**
   * Compute world matrices. With dt > 0 jiggle bones are simulated in fixed sub-steps; the
   * animated pose is their target, the simulated point mass adds the lag/overshoot on top.
   */
  update(dt: number): void {
    const SUB = 1 / 240;
    for (let i = 0; i < this.bones.length; i++) {
      const parent = this.parentIdx[i] >= 0 ? this.world[this.parentIdx[i]] : this.placement;
      const def = this.bones[i];
      let w = mul(parent, this.local(i, 0));
      if (def.jiggle) {
        const j = def.jiggle;
        const pivot = apply(w, def.pivot[0], def.pivot[1]);
        const target = apply(w, def.tip[0], def.tip[1]);
        let st = this.jig[i];
        if (!st || dt === 0) {
          st = { p: [...target], v: [0, 0], prevTarget: [...target], offset: 0 };
          this.jig[i] = st;
        }
        const scale = Math.hypot(w[0], w[1]); // screen px per art px
        const steps = Math.max(1, Math.round(dt / SUB));
        const h = dt / steps;
        const prev = st.prevTarget ?? target;
        const len = Math.hypot(target[0] - pivot[0], target[1] - pivot[1]);
        for (let k = 1; k <= steps && dt > 0; k++) {
          const f = k / steps;
          const tx = prev[0] + (target[0] - prev[0]) * f;
          const ty = prev[1] + (target[1] - prev[1]) * f;
          const ax = j.stiffness * (tx - st.p[0]) - j.damping * st.v[0];
          const ay = j.stiffness * (ty - st.p[1]) - j.damping * st.v[1] + (j.gravity ?? 0) * scale;
          st.v[0] += ax * h;
          st.v[1] += ay * h;
          st.p[0] += st.v[0] * h;
          st.p[1] += st.v[1] * h;
          // keep the bone length: the mass slides on a circle around the pivot
          const dx = st.p[0] - pivot[0];
          const dy = st.p[1] - pivot[1];
          const d = Math.hypot(dx, dy) || 1;
          st.p[0] = pivot[0] + (dx / d) * len;
          st.p[1] = pivot[1] + (dy / d) * len;
        }
        st.prevTarget = [...target];
        let off = Math.atan2(st.p[1] - pivot[1], st.p[0] - pivot[0]) - Math.atan2(target[1] - pivot[1], target[0] - pivot[0]);
        off = Math.atan2(Math.sin(off), Math.cos(off));
        off = Math.max(-j.limit, Math.min(j.limit, off));
        // the determinant sign flips the rotation direction when the placement mirrors
        st.offset = off;
        w = mul(parent, this.local(i, off));
      }
      this.world[i] = w;
    }
  }
}

/** A textured part: grid mesh over its rectangle, each vertex skinned to up to 4 bones. */
export interface PartMesh {
  name: string;
  rect: [number, number, number, number];
  /** rest positions in art px */
  rest: Float32Array;
  uv: Float32Array;
  indices: Uint16Array;
  bones: Uint8Array; // 4 per vertex
  weights: Float32Array; // 4 per vertex
  skinned: Float32Array; // output, screen px
}

export type WeightFn = (x: number, y: number) => [string, number][];

/**
 * Build a grid mesh over `rect` (art px), dropping cells that are fully transparent in `alpha`
 * (row-major, rect-sized). `cell` is the target cell size in art px.
 */
export function buildPartMesh(
  name: string,
  rect: [number, number, number, number],
  alpha: Uint8ClampedArray | null,
  cell: number,
  skel: Skeleton,
  weights: WeightFn,
): PartMesh {
  const [rx, ry, rw, rh] = rect;
  const cols = Math.max(1, Math.round(rw / cell));
  const rows = Math.max(1, Math.round(rh / cell));
  const keepCell = (cx: number, cy: number): boolean => {
    if (!alpha) return true;
    const x0 = Math.floor((cx / cols) * rw);
    const x1 = Math.ceil(((cx + 1) / cols) * rw);
    const y0 = Math.floor((cy / rows) * rh);
    const y1 = Math.ceil(((cy + 1) / rows) * rh);
    for (let y = Math.max(0, y0 - 1); y < Math.min(rh, y1 + 1); y++)
      for (let x = Math.max(0, x0 - 1); x < Math.min(rw, x1 + 1); x++) if (alpha[(y * rw + x) * 4 + 3] > 1) return true;
    return false;
  };
  const vid = new Int32Array((cols + 1) * (rows + 1)).fill(-1);
  const rest: number[] = [];
  const uv: number[] = [];
  const idx: number[] = [];
  const vert = (gx: number, gy: number): number => {
    const k = gy * (cols + 1) + gx;
    if (vid[k] < 0) {
      vid[k] = rest.length / 2;
      rest.push(rx + (gx / cols) * rw, ry + (gy / rows) * rh);
      uv.push(gx / cols, gy / rows);
    }
    return vid[k];
  };
  for (let cy = 0; cy < rows; cy++)
    for (let cx = 0; cx < cols; cx++) {
      if (!keepCell(cx, cy)) continue;
      const a = vert(cx, cy);
      const b = vert(cx + 1, cy);
      const c = vert(cx, cy + 1);
      const d = vert(cx + 1, cy + 1);
      idx.push(a, b, c, b, d, c);
    }
  const n = rest.length / 2;
  const bones = new Uint8Array(n * 4);
  const wts = new Float32Array(n * 4);
  for (let v = 0; v < n; v++) {
    const list = weights(rest[v * 2], rest[v * 2 + 1])
      .filter(([, w]) => w > 1e-4)
      .sort((p, q) => q[1] - p[1])
      .slice(0, 4);
    const sum = list.reduce((s, [, w]) => s + w, 0) || 1;
    list.forEach(([bn, w], k) => {
      const bi = skel.index.get(bn);
      if (bi === undefined) throw new Error(`weight to unknown bone ${bn}`);
      bones[v * 4 + k] = bi;
      wts[v * 4 + k] = w / sum;
    });
  }
  return {
    name,
    rect,
    rest: new Float32Array(rest),
    uv: new Float32Array(uv),
    indices: new Uint16Array(idx),
    bones,
    weights: wts,
    skinned: new Float32Array(rest.length),
  };
}

export function skin(mesh: PartMesh, skel: Skeleton): void {
  const { rest, bones, weights, skinned } = mesh;
  const W = skel.world;
  for (let v = 0, n = rest.length / 2; v < n; v++) {
    const x = rest[v * 2];
    const y = rest[v * 2 + 1];
    let ox = 0;
    let oy = 0;
    for (let k = 0; k < 4; k++) {
      const w = weights[v * 4 + k];
      if (w === 0) continue;
      const m = W[bones[v * 4 + k]];
      ox += w * (m[0] * x + m[2] * y + m[4]);
      oy += w * (m[1] * x + m[3] * y + m[5]);
    }
    skinned[v * 2] = ox;
    skinned[v * 2 + 1] = oy;
  }
}

/** Distance from p to segment ab, and the parameter along it (0..1). */
export function segDist(px: number, py: number, a: Vec2, b: Vec2): [number, number] {
  const vx = b[0] - a[0];
  const vy = b[1] - a[1];
  const l2 = vx * vx + vy * vy || 1;
  const t = Math.max(0, Math.min(1, ((px - a[0]) * vx + (py - a[1]) * vy) / l2));
  return [Math.hypot(px - (a[0] + t * vx), py - (a[1] + t * vy)), t];
}

export const ramp = (x: number, a: number, b: number): number => {
  const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
};
