/**
 * The relief rig's skeleton: a bone hierarchy with pivot-based local transforms, plus "jiggle"
 * bones - a damped point mass on a spring that the bone aims at - for the secondary motion of
 * ears, cheek fur, wings and tails.
 *
 * Everything is in pixels of the key art, y down. `update()` allocates nothing, because it runs
 * every frame: world matrices and spring states are preallocated per bone.
 */

import { type Affine, type Vec2, applyX, applyY, identity, mulInto } from './math';

export interface Jiggle {
  /** Spring constant (1/s²). Higher = snappier. */
  stiffness: number;
  /** Damping (1/s). Higher = less overshoot. */
  damping: number;
  /** Largest angle (radians) the spring may add on top of the animated pose. */
  limit: number;
  /** Downward pull in art px/s², scaled with the rig's on-screen size. */
  gravity: number;
}

export interface BoneDef {
  name: string;
  parent: string | null;
  pivot: Vec2;
  /** End of the bone: the spring's mass sits here. */
  tip: Vec2;
  jiggle?: Jiggle;
}

export interface BonePose {
  x: number;
  y: number;
  rot: number;
  sx: number;
  sy: number;
}

export class SkeletonError extends Error {}

/** Fixed physics sub-step: stable for the stiffest spring we ship at any frame rate. */
const SUB_STEP = 1 / 240;
/** A frame longer than this (a stalled tab) is simulated as this, not as a catapult. */
const MAX_DT = 0.1;

interface Spring {
  live: boolean;
  px: number;
  py: number;
  vx: number;
  vy: number;
  tx: number;
  ty: number;
  /** The angle the spring added last frame (radians), clamped to the bone's limit. */
  offset: number;
}

export class Skeleton {
  readonly bones: readonly BoneDef[];
  readonly index = new Map<string, number>();
  readonly pose: BonePose[];
  /** World matrix per bone (art px -> placed art px). */
  readonly world: Affine[];
  /** Placement of the whole rig, applied above the root. */
  placement: Affine = identity();
  private readonly parentIdx: Int32Array;
  private readonly springs: Spring[];
  private readonly local: Affine = identity();

  constructor(defs: readonly BoneDef[]) {
    if (defs.length === 0) throw new SkeletonError('a skeleton needs at least one bone');
    this.bones = defs;
    defs.forEach((b, i) => {
      if (this.index.has(b.name)) throw new SkeletonError(`duplicate bone ${b.name}`);
      this.index.set(b.name, i);
    });
    this.parentIdx = new Int32Array(defs.length);
    defs.forEach((b, i) => {
      if (b.parent === null) {
        if (i !== 0) throw new SkeletonError(`only the first bone may be the root, not ${b.name}`);
        this.parentIdx[i] = -1;
        return;
      }
      const p = this.index.get(b.parent);
      if (p === undefined) throw new SkeletonError(`bone ${b.name}: unknown parent ${b.parent}`);
      if (p >= i) throw new SkeletonError(`bone ${b.name} must come after its parent ${b.parent}`);
      this.parentIdx[i] = p;
    });
    if (defs[0].parent !== null) throw new SkeletonError('the first bone must be the root');
    this.pose = defs.map(() => ({ x: 0, y: 0, rot: 0, sx: 1, sy: 1 }));
    this.world = defs.map(identity);
    this.springs = defs.map(() => ({ live: false, px: 0, py: 0, vx: 0, vy: 0, tx: 0, ty: 0, offset: 0 }));
  }

  has(name: string): boolean {
    return this.index.has(name);
  }

  /** The pose of a bone, or `null` for a bone this creature does not have. */
  find(name: string): BonePose | null {
    const i = this.index.get(name);
    return i === undefined ? null : this.pose[i];
  }

  get(name: string): BonePose {
    const p = this.find(name);
    if (!p) throw new SkeletonError(`no bone ${name}`);
    return p;
  }

  resetPose(): void {
    for (const p of this.pose) {
      p.x = 0;
      p.y = 0;
      p.rot = 0;
      p.sx = 1;
      p.sy = 1;
    }
  }

  /** Forget all spring motion: the next update starts the springs at rest on their targets. */
  resetPhysics(): void {
    for (const s of this.springs) s.live = false;
  }

  /** The angle a jiggle bone's spring currently adds (radians); 0 for a plain bone. */
  springOffset(name: string): number {
    const i = this.index.get(name);
    return i === undefined ? 0 : this.springs[i].offset;
  }

  private writeLocal(i: number, extraRot: number): Affine {
    const b = this.bones[i];
    const p = this.pose[i];
    const px = b.pivot[0];
    const py = b.pivot[1];
    const c = Math.cos(p.rot + extraRot);
    const s = Math.sin(p.rot + extraRot);
    // T(pivot + offset) * R * S * T(-pivot)
    const m = this.local;
    m[0] = c * p.sx;
    m[1] = s * p.sx;
    m[2] = -s * p.sy;
    m[3] = c * p.sy;
    m[4] = px + p.x - (m[0] * px + m[2] * py);
    m[5] = py + p.y - (m[1] * px + m[3] * py);
    return m;
  }

  /**
   * Compute the world matrices. With `dt > 0` the jiggle bones are simulated in fixed sub-steps:
   * the animated pose is the spring's target, the simulated mass adds lag and overshoot on top.
   * `dt === 0` (a still frame, a reset) puts every spring at rest on its target.
   */
  update(dt: number): void {
    const step = Math.min(Math.max(dt, 0), MAX_DT);
    for (let i = 0; i < this.bones.length; i++) {
      const pi = this.parentIdx[i];
      const parent = pi >= 0 ? this.world[pi] : this.placement;
      const def = this.bones[i];
      const w = this.world[i];
      mulInto(w, parent, this.writeLocal(i, 0));
      const j = def.jiggle;
      if (!j) continue;
      const pvx = applyX(w, def.pivot[0], def.pivot[1]);
      const pvy = applyY(w, def.pivot[0], def.pivot[1]);
      const tgx = applyX(w, def.tip[0], def.tip[1]);
      const tgy = applyY(w, def.tip[0], def.tip[1]);
      const s = this.springs[i];
      if (!s.live || step === 0) {
        s.live = true;
        s.px = tgx;
        s.py = tgy;
        s.vx = 0;
        s.vy = 0;
        s.tx = tgx;
        s.ty = tgy;
        s.offset = 0;
        continue;
      }
      const scale = Math.hypot(w[0], w[1]);
      const len = Math.hypot(tgx - pvx, tgy - pvy);
      const steps = Math.max(1, Math.round(step / SUB_STEP));
      const h = step / steps;
      for (let k = 1; k <= steps; k++) {
        // the target moves continuously through the frame, not in one jump at its start
        const f = k / steps;
        const tx = s.tx + (tgx - s.tx) * f;
        const ty = s.ty + (tgy - s.ty) * f;
        const ax = j.stiffness * (tx - s.px) - j.damping * s.vx;
        const ay = j.stiffness * (ty - s.py) - j.damping * s.vy + j.gravity * scale;
        s.vx += ax * h;
        s.vy += ay * h;
        s.px += s.vx * h;
        s.py += s.vy * h;
        // the mass slides on a circle around the pivot: a spring never stretches a bone
        const dx = s.px - pvx;
        const dy = s.py - pvy;
        const d = Math.hypot(dx, dy) || 1;
        s.px = pvx + (dx / d) * len;
        s.py = pvy + (dy / d) * len;
      }
      s.tx = tgx;
      s.ty = tgy;
      let off = Math.atan2(s.py - pvy, s.px - pvx) - Math.atan2(tgy - pvy, tgx - pvx);
      off = Math.atan2(Math.sin(off), Math.cos(off));
      off = Math.max(-j.limit, Math.min(j.limit, off));
      s.offset = off;
      mulInto(w, parent, this.writeLocal(i, off));
    }
  }
}
