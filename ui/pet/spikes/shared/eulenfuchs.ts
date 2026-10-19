/**
 * Eulenfuchs rig (bones + skin weights) and its "brain": the procedural animation that turns the
 * shared script into a bone pose and face parameters each frame. Used by Rig 2.0 and Relief.
 */
import { type BoneDef, type Skeleton, type Vec2, type WeightFn, apply, ramp, segDist } from './rig';
import {
  type Envelope,
  TALK_START,
  clamp,
  hopPlan,
  lerp,
  lookTarget,
  mulberry32,
  noise1,
  smooth,
  window,
} from './timeline';

/** Art px from the ground point to the ear tips: the creature's height in the 512 art. */
export const ART_HEIGHT = 375;
export const GROUND: Vec2 = [260, 505];

const J = (stiffness: number, damping: number, limit: number, gravity = 0) => ({ stiffness, damping, limit, gravity });

export const BONES: BoneDef[] = [
  { name: 'root', parent: null, pivot: GROUND, tip: [260, 460] },
  { name: 'hips', parent: 'root', pivot: [260, 462], tip: [260, 400] },
  { name: 'spine', parent: 'hips', pivot: [260, 420], tip: [260, 340] },
  { name: 'chest', parent: 'spine', pivot: [260, 372], tip: [260, 318] },
  { name: 'leg_l', parent: 'chest', pivot: [224, 388], tip: [224, 500] },
  { name: 'leg_r', parent: 'chest', pivot: [290, 388], tip: [290, 500] },
  { name: 'wing_l', parent: 'chest', pivot: [178, 322], tip: [160, 392], jiggle: J(240, 13, 0.45) },
  { name: 'wing_r', parent: 'chest', pivot: [342, 322], tip: [360, 392], jiggle: J(240, 13, 0.45) },
  { name: 'neck', parent: 'chest', pivot: [260, 330], tip: [260, 300] },
  { name: 'head', parent: 'neck', pivot: [260, 318], tip: [260, 200], jiggle: J(520, 26, 0.1) },
  { name: 'jaw', parent: 'head', pivot: [260, 301], tip: [260, 318] },
  { name: 'cheek_l', parent: 'head', pivot: [190, 262], tip: [166, 302], jiggle: J(300, 11, 0.28) },
  { name: 'cheek_r', parent: 'head', pivot: [330, 262], tip: [354, 302], jiggle: J(300, 11, 0.28) },
  { name: 'ear_l', parent: 'head', pivot: [194, 204], tip: [180, 166], jiggle: J(420, 15, 0.35) },
  { name: 'ear_l2', parent: 'ear_l', pivot: [180, 166], tip: [171, 136], jiggle: J(260, 9, 0.5) },
  { name: 'ear_r', parent: 'head', pivot: [326, 204], tip: [340, 166], jiggle: J(420, 15, 0.35) },
  { name: 'ear_r2', parent: 'ear_r', pivot: [340, 166], tip: [351, 136], jiggle: J(260, 9, 0.5) },
  { name: 'tail_l0', parent: 'hips', pivot: [195, 452], tip: [150, 440], jiggle: J(130, 9, 0.5) },
  { name: 'tail_l1', parent: 'tail_l0', pivot: [150, 440], tip: [115, 455], jiggle: J(95, 7, 0.55) },
  { name: 'tail_l2', parent: 'tail_l1', pivot: [115, 455], tip: [86, 482], jiggle: J(75, 6, 0.6) },
  { name: 'tail_r0', parent: 'hips', pivot: [348, 466], tip: [385, 455], jiggle: J(150, 10, 0.45) },
  { name: 'tail_r1', parent: 'tail_r0', pivot: [385, 455], tip: [422, 470], jiggle: J(110, 8, 0.5) },
];

const hat = (f: number, k: number, n: number): number => {
  if (k === 0 && f <= 0.5) return 1;
  if (k === n - 1 && f >= n - 0.5) return 1;
  return Math.max(0, 1 - Math.abs(f - (k + 0.5)));
};

function chain(points: Vec2[], bones: string[], base: string, baseLen = 0.12): WeightFn {
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
    return [[base, wb] as [string, number], ...bones.map((b, k) => [b, (1 - wb) * hat(f, k, bones.length)] as [string, number])];
  };
}

/** Skin weights per part, as functions of the rest position in art px. */
export const WEIGHTS: Record<string, WeightFn> = {
  body: (x, y) => {
    const leg = (cx: number) => (1 - ramp(Math.abs(x - cx), 12, 22)) * ramp(y, 392, 428);
    const ll = leg(224);
    const lr = leg(290);
    const rest = 1 - ll - lr;
    const ys = [300, 345, 405, 462];
    const names = ['neck', 'chest', 'spine', 'hips'];
    const out: [string, number][] = [['leg_l', ll], ['leg_r', lr]];
    for (let k = 0; k < 4; k++) {
      const lo = k === 0 ? -Infinity : ys[k - 1];
      const hi = k === 3 ? Infinity : ys[k + 1];
      let w: number;
      if (y <= ys[k]) w = k === 0 ? 1 : clamp((y - lo) / (ys[k] - lo));
      else w = k === 3 ? 1 : clamp((hi - y) / (hi - ys[k]));
      out.push([names[k], w * rest]);
    }
    return out;
  },
  head: (x, y) => {
    const cw = ramp(Math.abs(x - 260), 70, 96) * ramp(y, 225, 268);
    const nw = 0.75 * ramp(y, 298, 332);
    const hw = Math.max(0, 1 - cw - nw);
    return [['head', hw], [x < 260 ? 'cheek_l' : 'cheek_r', cw], ['neck', nw]];
  },
  ear_l: earWeights([194, 206], [171, 136], 'ear_l', 'ear_l2'),
  ear_r: earWeights([326, 206], [351, 136], 'ear_r', 'ear_r2'),
  jaw: () => [['jaw', 1]],
  wing_l: wingWeights([178, 318], [160, 395], 'wing_l'),
  wing_r: wingWeights([342, 318], [360, 395], 'wing_r'),
  tail_l: chain([[198, 452], [150, 440], [115, 455], [86, 482]], ['tail_l0', 'tail_l1', 'tail_l2'], 'hips', 0.08),
  tail_r: chain([[346, 466], [385, 455], [424, 470]], ['tail_r0', 'tail_r1'], 'hips', 0.1),
};

function earWeights(base: Vec2, tip: Vec2, b1: string, b2: string): WeightFn {
  return (x, y) => {
    const [, s] = segDist(x, y, base, tip);
    const hw = 1 - ramp(s, -0.05, 0.14);
    const w2 = (1 - hw) * ramp(s, 0.35, 0.72);
    return [['head', hw], [b1, 1 - hw - w2], [b2, w2]];
  };
}

function wingWeights(base: Vec2, tip: Vec2, b: string): WeightFn {
  return (x, y) => {
    const [, s] = segDist(x, y, base, tip);
    const cw = 1 - ramp(s, 0, 0.3);
    return [['chest', cw], [b, 1 - cw]];
  };
}

export interface Face {
  /** Iris offset inside the eye, art px. */
  iris: Vec2;
  lidL: number;
  lidR: number;
  /** Lower lid (smiling eyes), 0..1. */
  lower: number;
  mouth: number;
  /** Head turn, -1..1 (right/down positive): Rig 2.0 fakes it with parallax, Relief rotates. */
  yaw: number;
  pitch: number;
  bodyYaw: number;
  /** 0 awake .. 1 asleep (head pitch for the 2.5D renderer). */
  sleep: number;
  shadowScale: number;
  shadowAlpha: number;
}

const EAR_TWITCHES: [number, 'l' | 'r'][] = [
  [1.25, 'r'], [2.45, 'l'], [4.3, 'r'], [5.6, 'l'], [16.2, 'r'], [17.6, 'l'],
];

/** Fast flick out and slower return. */
const twitch = (dt: number): number => (dt < 0 || dt > 0.45 ? 0 : dt < 0.05 ? dt / 0.05 : Math.exp(-(dt - 0.05) * 9));

export class Brain {
  private rng = mulberry32(42);
  private breath = 0;
  private nextBlink = 1.4;
  private blinkAt = -10;
  private doubleAt = -10;
  private gaze: Vec2 = [0, 0];
  private gazeGoal: Vec2 = [0, 0];
  private nextSaccade = 0.7;
  private head: Vec2 = [0, 0];
  private headV: Vec2 = [0, 0];
  private mouth = 0;
  headScreen: Vec2 = [0, 0];

  reset(): void {
    this.rng = mulberry32(42);
    this.breath = 0;
    this.nextBlink = 1.4;
    this.blinkAt = this.doubleAt = -10;
    this.gaze = [0, 0];
    this.gazeGoal = [0, 0];
    this.nextSaccade = 0.7;
    this.head = [0, 0];
    this.headV = [0, 0];
    this.mouth = 0;
  }

  /**
   * Write the pose for script time t into `skel` and return face parameters.
   * `view` is the canvas size and creature height in px, used to aim at the look target.
   */
  update(t: number, dt: number, skel: Skeleton, env: Envelope, view: { w: number; h: number; H: number }): Face {
    skel.resetPose();
    const P = (n: string) => skel.get(n);
    const root = P('root');
    const chest = P('chest');
    const spine = P('spine');
    const head = P('head');
    const jaw = P('jaw');

    // ---- state weights ----------------------------------------------------------------
    const settle = window(t, 9.5, 10.3, 13.15, 13.9);
    const asleep = window(t, 9.75, 11.0, 13.0, 13.55);
    const yawn = window(t, 13.1, 13.4, 13.65, 14.0);
    const stretch = window(t, 13.35, 13.65, 13.85, 14.15);
    const happy = window(t, 13.95, 14.15, 14.85, 15.2);
    const talking = t >= TALK_START - 0.1;
    const hop = hopPlan(t);
    const look = lookTarget(t);

    // ---- breathing ----------------------------------------------------------------------
    const rate = lerp(0.3, 0.19, asleep) + 0.3 * happy;
    this.breath += 2 * Math.PI * rate * dt;
    const amp = 1 + 1.1 * asleep + 0.5 * happy;
    const b = Math.sin(this.breath);
    chest.sx *= 1 + 0.009 * amp * b;
    chest.sy *= 1 + 0.016 * amp * b;
    spine.sy *= 1 + 0.006 * amp * b;
    head.y -= 1.1 * amp * b;

    // ---- gaze & head aim ----------------------------------------------------------------
    if (t >= this.nextSaccade) {
      const r = this.rng;
      if (r() < 0.55) this.gazeGoal = [(r() - 0.5) * 0.2, (r() - 0.5) * 0.14];
      else this.gazeGoal = [(r() - 0.5) * 1.0, (r() - 0.6) * 0.5];
      this.nextSaccade = t + 0.6 + r() * 1.7;
    }
    let goal: Vec2 = [...this.gazeGoal];
    if (look.w > 0) {
      const dx = (look.u * view.w - this.headScreen[0]) / (view.H * 0.55);
      const dy = (look.v * view.h - this.headScreen[1]) / (view.H * 0.45);
      const lx = clamp(dx, -1, 1);
      const ly = clamp(dy, -1, 1);
      // pursuit by catch-up saccades: the eyes jump when the error grows, never glide
      const err = Math.hypot(lx - this.gaze[0], ly - this.gaze[1]);
      const pursuit: Vec2 = err > 0.09 ? [lx, ly] : [...this.gaze];
      goal = [lerp(goal[0], pursuit[0], look.w), lerp(goal[1], pursuit[1], look.w)];
    }
    if (hop.active > 0) goal = [lerp(goal[0], hop.dir * 0.55, hop.active), lerp(goal[1], 0.15, hop.active)];
    const down = Math.max(settle, asleep);
    goal = [lerp(goal[0], 0, down), lerp(goal[1], 0.6, down)];
    if (talking) goal = [goal[0] * 0.35, goal[1] * 0.35];
    const ke = 1 - Math.exp(-dt / 0.028); // saccade: ~30 ms
    this.gaze[0] += (goal[0] - this.gaze[0]) * ke;
    this.gaze[1] += (goal[1] - this.gaze[1]) * ke;
    // head follows the gaze goal with a critically damped spring (eyes lead, head follows)
    const w0 = 7.5;
    for (let k = 0; k < 2; k++) {
      const a = w0 * w0 * (goal[k] * 0.8 - this.head[k]) - 2 * w0 * this.headV[k];
      this.headV[k] += a * dt;
      this.head[k] += this.headV[k] * dt;
    }
    const hx = this.head[0];
    const hy = this.head[1];
    head.x += 6 * hx;
    head.y += 4 * hy;
    head.rot += 0.05 * hx;
    jaw.x += 2.5 * hx;
    jaw.y += 1.2 * hy;

    // ---- idle life ----------------------------------------------------------------------
    root.rot += 0.01 * noise1(t * 0.35, 1);
    head.rot += 0.015 * noise1(t * 0.55, 3);
    head.x += 1.2 * noise1(t * 0.5, 5);
    const tw = 2 * Math.PI * 0.3 * t;
    P('tail_l0').rot += 0.06 * Math.sin(tw);
    P('tail_l1').rot += 0.08 * Math.sin(tw - 0.9);
    P('tail_l2').rot += 0.1 * Math.sin(tw - 1.8);
    P('tail_r0').rot += 0.05 * Math.sin(tw * 0.9 + 0.6);
    P('tail_r1').rot += 0.07 * Math.sin(tw * 0.9 - 0.3);
    for (const [at, side] of EAR_TWITCHES) {
      const k = twitch(t - at) * (1 - asleep);
      if (side === 'l') P('ear_l').rot -= 0.3 * k;
      else P('ear_r').rot += 0.3 * k;
    }

    // ---- hop ------------------------------------------------------------------------------
    root.x += hop.x * ART_HEIGHT;
    root.y -= hop.lift * ART_HEIGHT;
    root.sy *= 1 + 0.11 * hop.squash;
    root.sx *= 1 - 0.075 * hop.squash;
    root.rot += hop.dir * 0.07 * hop.air;
    P('leg_l').y -= 13 * hop.air;
    P('leg_r').y -= 13 * hop.air;
    P('leg_l').rot += 0.08 * hop.air;
    P('leg_r').rot -= 0.08 * hop.air;
    P('wing_l').rot += 0.38 * hop.air;
    P('wing_r').rot -= 0.38 * hop.air;
    head.y += 4 * Math.min(0, hop.squash); // head dips into the landing

    // ---- settle & sleep ---------------------------------------------------------------------
    root.sy *= 1 - 0.035 * settle;
    root.sx *= 1 + 0.02 * settle;
    chest.y += 4 * settle;
    P('neck').rot += 0.04 * asleep;
    const nod = window(t, 10.35, 10.55, 10.6, 10.95); // dozes off, catches itself, sinks again
    head.y += 15 * asleep + 6 * nod;
    head.rot -= 0.1 * asleep;
    head.sy *= 1 - 0.035 * asleep;
    P('ear_l').rot -= 0.22 * asleep;
    P('ear_r').rot += 0.22 * asleep;
    P('wing_l').rot -= 0.06 * asleep;
    P('wing_r').rot += 0.06 * asleep;
    P('tail_l0').rot += 0.1 * asleep;
    P('tail_l1').rot += 0.18 * asleep;
    P('tail_l2').rot += 0.24 * asleep;

    // ---- wake: yawn, stretch, happy -----------------------------------------------------------
    head.rot += 0.06 * yawn;
    head.y -= 5 * yawn;
    chest.sy *= 1 + 0.03 * stretch;
    root.sy *= 1 + 0.02 * stretch;
    P('ear_l').rot -= 0.15 * stretch;
    P('ear_r').rot += 0.15 * stretch;
    const bounce = Math.max(0, Math.sin(2 * Math.PI * 2.3 * (t - 13.95)));
    root.y -= 0.03 * ART_HEIGHT * bounce * happy;
    root.sy *= 1 + 0.05 * happy * (bounce - 0.3);
    const wag = Math.sin(2 * Math.PI * 3.3 * t);
    P('tail_r0').rot += 0.3 * wag * happy;
    P('tail_l0').rot += 0.2 * Math.sin(2 * Math.PI * 3.3 * t + 1) * happy;
    P('tail_l1').rot += 0.25 * Math.sin(2 * Math.PI * 3.3 * t + 0.2) * happy;
    P('ear_l').rot += 0.12 * happy;
    P('ear_r').rot -= 0.12 * happy;
    const flutter = 0.22 + 0.16 * Math.sin(2 * Math.PI * 7 * t);
    P('wing_l').rot += flutter * happy;
    P('wing_r').rot -= flutter * happy;
    head.rot += 0.09 * Math.sin(2 * Math.PI * 1.1 * (t - 13.95)) * happy;

    // ---- talking: the jaw follows the audio envelope ---------------------------------------------
    const raw = talking ? Math.pow(env.at(t - TALK_START), 0.75) : 0;
    const tau = raw > this.mouth ? 0.025 : 0.07; // fast attack, softer release
    this.mouth += (raw - this.mouth) * (1 - Math.exp(-dt / tau));
    let mouth = Math.max(this.mouth, yawn * 1.25, 0.22 * happy);
    if (dt === 0) mouth = 0;
    jaw.y += 10 * mouth;
    jaw.rot += 0.03 * mouth * noise1(t * 2, 11); // a little asymmetry
    jaw.sy *= 1 + 0.12 * mouth;
    head.y -= 1.6 * this.mouth;
    head.rot += 0.03 * noise1(t * 1.4, 9) * (talking ? 1 : 0);
    P('ear_l').rot += 0.06 * this.mouth;
    P('ear_r').rot -= 0.06 * this.mouth;

    // ---- lids -----------------------------------------------------------------------------------
    if (t >= this.nextBlink) {
      this.blinkAt = t;
      this.doubleAt = this.rng() < 0.25 ? t + 0.3 : -10;
      this.nextBlink = t + 1.8 + this.rng() * 2.8;
    }
    const blinkCurve = (s: number): number =>
      s < 0 ? 0 : s < 0.07 ? s / 0.07 : s < 0.11 ? 1 : s < 0.26 ? 1 - (s - 0.11) / 0.15 : 0;
    const blink = Math.max(blinkCurve(t - this.blinkAt), blinkCurve(t - this.doubleAt));
    const drowsy = 0.4 * window(t, 13.0, 13.2, 13.9, 14.3) + 0.3 * settle;
    const lid = clamp(Math.max(blink, asleep, yawn * 0.9, drowsy, 0.28 * happy, 0.18 * Math.max(0, this.gaze[1])));
    const lower = clamp(0.75 * happy + 0.5 * yawn + 0.25 * this.mouth);

    // ---- shadow -------------------------------------------------------------------------------
    const lift = hop.lift + 0.03 * bounce * happy;

    skel.update(dt);
    const hs = apply(skel.world[skel.index.get('head')!], 260, 245);
    this.headScreen = hs;

    return {
      iris: [clamp(this.gaze[0] - 0.55 * hx, -1, 1) * 3.2, clamp(this.gaze[1] - 0.55 * hy, -1, 1) * 2.4],
      lidL: lid,
      lidR: lid,
      lower,
      mouth,
      yaw: hx,
      pitch: hy,
      bodyYaw: hop.dir * 0.6 * hop.air + 0.15 * hx,
      sleep: asleep,
      shadowScale: 1 - 1.8 * lift,
      shadowAlpha: 1 - 2.6 * lift,
    };
  }
}

export const smoothstep = smooth;
