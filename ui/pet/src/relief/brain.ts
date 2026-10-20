/**
 * The relief creature's "brain": turns the eased state targets into a bone pose and face
 * parameters, once per frame.
 *
 * Layered like a real animal: breathing and idle drift underneath; posture (ears, chin, crouch)
 * held by the state; eyes that jump in saccades with the head following on a spring; one-shot
 * moments on top (blink, ear twitch, startle, yawn when waking). Every state channel is eased, so
 * no state change is ever a jump; the springs in the skeleton add the overshoot.
 *
 * The brain talks to bones by role (`relief.json` "roles"), so creatures without wings or with one
 * tail are animated by the same code; a role a creature lacks is simply skipped.
 */

import { type Vec2, approach, applyX, applyY, clamp, lerp, mulberry32, noise1 } from './math';
import type { ReliefDefinition } from './reliefFile';
import type { BonePose, Skeleton } from './skeleton';
import { SpeechEnvelope } from './speech';
import type { ReliefTargets } from './stateMapping';

export interface Face {
  /** Iris offset inside the eye, art px. */
  iris: Vec2;
  lid: number;
  lower: number;
  mouth: number;
  /** Head turn, -1..1 (right / down positive). The renderer turns it into a 3D rotation. */
  yaw: number;
  pitch: number;
  bodyYaw: number;
  /** 0 awake .. 1 asleep (extra head pitch). */
  sleep: number;
  /** Height above ground, fraction of the creature height (for the contact shadow). */
  lift: number;
  light: number;
}

type Num = Exclude<keyof ReliefTargets, 'gaze' | 'ambient'>;

/** Ease time constants per channel (seconds): posture is slow, lids and ears are quick. */
const TAU: Record<Num, number> = {
  lid: 0.12,
  lower: 0.2,
  asleep: 1.1,
  ears: 0.22,
  earTwist: 0.3,
  tilt: 0.4,
  lift: 0.45,
  wander: 0.5,
  attention: 0.3,
  bounce: 0.35,
  bounceRate: 0.5,
  flutter: 0.25,
  wag: 0.3,
  sway: 0.6,
  breathRate: 1.2,
  breathAmp: 1.2,
  tremble: 0.2,
  crouch: 0.4,
  talk: 0.08,
  speech: 0.1,
  puff: 0.5,
  light: 0.6,
};
const CHANNELS = Object.keys(TAU) as Num[];

/** Fast flick out and a slower return. */
const twitch = (s: number): number =>
  s < 0 || s > 0.45 ? 0 : s < 0.05 ? s / 0.05 : Math.exp(-(s - 0.05) * 9);
/** 0 before a, up to 1 at b, held to c, down to 0 at d (all relative). */
function env(s: number, a: number, b: number, c: number, d: number): number {
  if (s <= a || s >= d) return 0;
  if (s < b) return (s - a) / (b - a);
  if (s <= c) return 1;
  return 1 - (s - c) / (d - c);
}
const blinkCurve = (s: number): number =>
  s < 0 ? 0 : s < 0.07 ? s / 0.07 : s < 0.11 ? 1 : s < 0.26 ? 1 - (s - 0.11) / 0.15 : 0;

/**
 * One bounce cycle at phase k in [0, 1): anticipation squash, airborne stretch, landing squash.
 * Returns lift (0..1 of the hop height), squash (<0 squash, >0 stretch) and air (0..1).
 */
export function hopShape(k: number): { lift: number; squash: number; air: number } {
  if (k < 0.18) return { lift: 0, squash: -Math.sin((k / 0.18) * Math.PI * 0.5) * 0.8, air: 0 };
  if (k < 0.72) {
    const a = (k - 0.18) / 0.54;
    return {
      lift: 4 * a * (1 - a),
      squash: a < 0.5 ? 0.8 * (1 - 2 * a) : 0.4 * (2 * a - 1),
      air: Math.sin(a * Math.PI),
    };
  }
  const l = (k - 0.72) / 0.28;
  return { lift: 0, squash: -Math.sin(Math.min(1, l * 1.6) * Math.PI) * (1 - l) * 0.9, air: 0 };
}

export class ReliefBrain {
  private readonly def: ReliefDefinition;
  private readonly seed: number;
  private rng: () => number;
  private readonly speech: SpeechEnvelope;
  private cur: ReliefTargets | null = null;
  private gazeBias: Vec2 = [0, 0];
  private breath = 0;
  private bouncePhase = 0;
  private tailPhase = 0;
  private nextBlink = 1.2;
  private blinkAt = -10;
  private doubleAt = -10;
  private gaze: Vec2 = [0, 0];
  private gazeGoal: Vec2 = [0, 0];
  private nextSaccade = 0.6;
  private glance: Vec2 = [0, 0];
  private glanceUntil = 0;
  private nextGlance = 3;
  private head: Vec2 = [0, 0];
  private headV: Vec2 = [0, 0];
  private nextTwitch = 2;
  private twitchAt = -10;
  private twitchSide = 1;
  private startleAt = -10;
  private wakeAt = -10;
  private nodAt = -10;
  private wasAsleep = false;
  /** Where the look anchor (face) is on screen after the last update, in placed art px. */
  readonly anchor: Vec2 = [0, 0];

  constructor(def: ReliefDefinition, seed = 42) {
    this.def = def;
    this.seed = seed;
    this.rng = mulberry32(seed);
    this.speech = new SpeechEnvelope(seed ^ 0x5eed);
  }

  reset(): void {
    this.rng = mulberry32(this.seed);
    this.speech.reset();
    this.cur = null;
    this.breath = this.bouncePhase = this.tailPhase = 0;
    this.nextBlink = 1.2;
    this.blinkAt = this.doubleAt = this.twitchAt = this.startleAt = this.wakeAt = this.nodAt = -10;
    this.gaze = [0, 0];
    this.gazeGoal = [0, 0];
    this.gazeBias = [0, 0];
    this.nextSaccade = 0.6;
    this.glance = [0, 0];
    this.glanceUntil = 0;
    this.nextGlance = 3;
    this.head = [0, 0];
    this.headV = [0, 0];
    this.nextTwitch = 2;
    this.wasAsleep = false;
  }

  /** Something happened *to* the creature (kill switch, hard error): flinch. */
  startle(t: number): void {
    this.startleAt = t;
  }

  /** The eased value of a channel (for tests and for the renderer's light). */
  channel(name: Num): number {
    return this.cur ? this.cur[name] : 0;
  }

  /**
   * Write the pose for time `t` (seconds) into `skel` and return the face. `dt === 0` jumps every
   * eased channel straight to its target (first frame, still frames).
   */
  update(t: number, rawDt: number, skel: Skeleton, target: ReliefTargets): Face {
    // the caller clamps too; this keeps a bad step (negative, NaN, a stalled tab) out of every
    // integrator below even when the brain is driven from somewhere else
    const dt = Number.isFinite(rawDt) ? Math.min(0.1, Math.max(0, rawDt)) : 0;
    const c = this.ease(target, dt);
    const roles = this.def.roles;
    const H = this.def.artHeight;
    skel.resetPose();
    const bone = (n: string): BonePose | null => skel.find(n);
    const root = skel.get('root');
    const chest = skel.get('chest');
    const spine = skel.get('spine');
    const neck = skel.get('neck');
    const head = skel.get('head');
    const jaw = skel.get('jaw');
    const wingL = roles.wings ? bone(roles.wings.left) : null;
    const wingR = roles.wings ? bone(roles.wings.right) : null;
    const earsL = roles.ears.left.map((n) => skel.get(n));
    const earsR = roles.ears.right.map((n) => skel.get(n));
    const ears = (l: number, r: number, tipFactor = 0): void => {
      earsL.forEach((p, i) => (p.rot += l * (i === 0 ? 1 : tipFactor)));
      earsR.forEach((p, i) => (p.rot += r * (i === 0 ? 1 : tipFactor)));
    };
    const wings = (l: number, r: number): void => {
      if (wingL) wingL.rot += l;
      if (wingR) wingR.rot += r;
    };

    // ---- one-shots and their triggers ---------------------------------------------------------
    const asleepNow = target.asleep > 0.5;
    if (asleepNow && !this.wasAsleep) this.nodAt = t + 0.6;
    if (!asleepNow && this.wasAsleep && dt > 0) this.wakeAt = t;
    this.wasAsleep = asleepNow;
    const ws = t - this.wakeAt;
    const yawn = env(ws, 0.1, 0.45, 0.75, 1.15);
    const stretch = env(ws, 0.4, 0.7, 0.95, 1.3);
    const nod = env(t - this.nodAt, 0, 0.2, 0.25, 0.6);
    const ss = t - this.startleAt;
    const startle = env(ss, 0, 0.06, 0.18, 0.7);
    const startleSquash = ss >= 0 && ss < 0.12 ? -Math.sin((ss / 0.12) * Math.PI) : 0;

    // ---- breathing ------------------------------------------------------------------------------
    const rate = lerp(c.breathRate, 0.19, c.asleep);
    this.breath += 2 * Math.PI * rate * dt;
    const amp = (c.breathAmp / 0.045) * (1 + 1.1 * c.asleep);
    const b = Math.sin(this.breath);
    chest.sx *= 1 + 0.009 * amp * b;
    chest.sy *= 1 + 0.016 * amp * b;
    spine.sy *= 1 + 0.006 * amp * b;
    head.y -= 1.1 * amp * b;

    // ---- gaze & head aim (eyes lead, head follows) ----------------------------------------------
    const r = this.rng;
    if (t >= this.nextSaccade) {
      const w = c.wander;
      if (r() < 0.6) this.gazeGoal = [(r() - 0.5) * 0.2 * w, (r() - 0.5) * 0.14 * w];
      else this.gazeGoal = [(r() - 0.5) * 0.9 * w, (r() - 0.6) * 0.45 * w];
      this.nextSaccade = t + 0.6 + r() * 1.7;
    }
    if (target.ambient === 'full' && c.wander > 0.3 && t >= this.nextGlance) {
      // a look around: a real head turn to one side, held a moment
      const side = r() < 0.5 ? -1 : 1;
      this.glance = [side * (0.55 + 0.3 * r()), (r() - 0.6) * 0.4];
      this.glanceUntil = t + 0.9 + r() * 1.4;
      this.nextGlance = this.glanceUntil + 3 + (r() * 6) / Math.max(0.3, c.wander);
    }
    const glancing = t < this.glanceUntil && target.ambient === 'full' ? 1 : 0;
    this.gazeBias[0] += (target.gaze[0] - this.gazeBias[0]) * approach(dt, 0.4);
    this.gazeBias[1] += (target.gaze[1] - this.gazeBias[1]) * approach(dt, 0.4);
    if (dt === 0) this.gazeBias = [...target.gaze];
    let gx = this.gazeBias[0] + this.gazeGoal[0] + glancing * this.glance[0];
    let gy = this.gazeBias[1] + this.gazeGoal[1] + glancing * this.glance[1];
    gx *= 1 - 0.65 * c.attention;
    gy *= 1 - 0.65 * c.attention;
    gy = lerp(gy, 0.6, c.asleep);
    gx = lerp(gx, 0, c.asleep);
    const ke = dt === 0 ? 1 : approach(dt, 0.028); // a saccade takes ~30 ms
    this.gaze[0] += (gx - this.gaze[0]) * ke;
    this.gaze[1] += (gy - this.gaze[1]) * ke;
    const w0 = 7.5; // critically damped head spring
    for (let k = 0; k < 2; k++) {
      const goal = (k === 0 ? gx : gy) * 0.8;
      if (dt === 0) {
        this.head[k] = goal;
        this.headV[k] = 0;
        continue;
      }
      const a = w0 * w0 * (goal - this.head[k]) - 2 * w0 * this.headV[k];
      this.headV[k] += a * dt;
      this.head[k] += this.headV[k] * dt;
    }
    const hx = this.head[0];
    const hy = this.head[1] - 0.5 * c.lift;
    head.x += 6 * hx;
    head.y += 4 * hy;
    head.rot += 0.05 * hx + 0.18 * c.tilt;
    jaw.x += 2.5 * hx;
    jaw.y += 1.2 * hy;

    // ---- idle drift, tails ------------------------------------------------------------------------
    const awake = 1 - c.asleep;
    root.rot += 0.01 * noise1(t * 0.35, 1) * awake;
    head.rot += 0.015 * noise1(t * 0.55, 3) * awake;
    head.x += 1.2 * noise1(t * 0.5, 5) * awake;
    this.tailPhase += 2 * Math.PI * 0.3 * dt;
    const wagT = 2 * Math.PI * 3.3 * t;
    roles.tails.forEach((chain, ti) => {
      chain.forEach((n, k) => {
        const p = skel.get(n);
        const lag = k * 0.9 + ti * 0.6;
        p.rot += c.sway * (0.06 + 0.02 * k) * Math.sin(this.tailPhase * (ti ? 0.9 : 1) - lag);
        p.rot += c.wag * (k === 0 ? 0.3 : 0.22) * Math.sin(wagT + (ti ? 1 : 0) - 0.8 * k);
        p.rot += c.asleep * (0.1 + 0.07 * k) * (ti ? -0.5 : 1); // curl in when asleep
      });
    });

    // ---- ear twitches (ambient) --------------------------------------------------------------------
    if (target.ambient === 'full' && t >= this.nextTwitch) {
      this.twitchAt = t;
      this.twitchSide = r() < 0.5 ? -1 : 1;
      this.nextTwitch = t + 2.5 + r() * 5;
    }
    const tw = twitch(t - this.twitchAt) * awake;
    if (this.twitchSide < 0) ears(-0.3 * tw, 0);
    else ears(0, 0.3 * tw);

    // ---- posture --------------------------------------------------------------------------------------
    // ears: + flat back/outward, - pricked; twist: one up, one down
    ears(-0.3 * c.ears + 0.25 * c.earTwist, 0.3 * c.ears + 0.25 * c.earTwist, 0.6);
    for (const p of [...earsL, ...earsR]) p.sy *= 1 - 0.12 * Math.max(0, c.ears);
    root.sy *= 1 - 0.05 * c.crouch + 0.02 * c.puff;
    chest.y += 5 * c.crouch;
    chest.sx *= 1 + 0.03 * c.puff - 0.02 * c.crouch;
    head.y += 8 * c.crouch - 3 * c.puff;
    wings(-0.12 * c.crouch, 0.12 * c.crouch);

    // ---- trembling ---------------------------------------------------------------------------------
    if (c.tremble > 0.01) {
      root.x += 1.4 * c.tremble * noise1(t * 19, 13);
      head.rot += 0.012 * c.tremble * noise1(t * 23, 17);
    }

    // ---- bounce (happy, celebrating, hype) -----------------------------------------------------------
    this.bouncePhase += c.bounceRate * dt;
    let lift = 0;
    let air = 0;
    if (c.bounce > 0.01) {
      const hop = hopShape(this.bouncePhase % 1);
      lift = 0.07 * c.bounce * hop.lift;
      air = hop.air * c.bounce;
      root.y -= lift * H;
      root.sy *= 1 + 0.1 * c.bounce * hop.squash;
      root.sx *= 1 - 0.07 * c.bounce * hop.squash;
      for (const n of roles.legs) {
        const p = skel.get(n);
        p.y -= 10 * air;
      }
      wings(0.38 * air, -0.38 * air);
      head.y += 4 * Math.min(0, hop.squash) * c.bounce;
    }
    const flutter = c.flutter * (0.22 + 0.16 * Math.sin(2 * Math.PI * 7 * t));
    wings(flutter, -flutter);

    // ---- asleep, nod-off, waking --------------------------------------------------------------------
    root.sy *= 1 - 0.035 * c.asleep;
    root.sx *= 1 + 0.02 * c.asleep;
    chest.y += 4 * c.asleep;
    neck.rot += 0.04 * c.asleep;
    head.y += 15 * c.asleep + 6 * nod;
    head.rot -= 0.1 * c.asleep;
    head.sy *= 1 - 0.035 * c.asleep;
    ears(-0.22 * c.asleep, 0.22 * c.asleep);
    wings(-0.06 * c.asleep, 0.06 * c.asleep);
    head.rot += 0.06 * yawn;
    head.y -= 5 * yawn;
    chest.sy *= 1 + 0.03 * stretch;
    root.sy *= 1 + 0.02 * stretch;
    ears(-0.15 * stretch, 0.15 * stretch);

    // ---- startle -------------------------------------------------------------------------------------
    root.sy *= 1 + 0.08 * startleSquash + 0.03 * startle;
    root.sx *= 1 - 0.05 * startleSquash;
    ears(0.35 * startle, -0.35 * startle);
    head.y -= 4 * startle;

    // ---- talking: the jaw follows the speech envelope -------------------------------------------------
    const env0 = this.speech.sample(t, dt, c.talk > 0.05 ? c.speech * c.talk : 0);
    let mouth = Math.max(env0, yawn * 1.25, 0.18 * c.lower * (1 - c.talk));
    if (dt === 0 && c.talk < 0.05) mouth = Math.max(yawn, 0.18 * c.lower);
    jaw.y += 10 * mouth;
    jaw.rot += 0.03 * mouth * noise1(t * 2, 11);
    jaw.sy *= 1 + 0.12 * mouth;
    head.y -= 1.6 * env0;
    head.rot += 0.03 * noise1(t * 1.4, 9) * c.talk;
    ears(0.06 * env0, -0.06 * env0);

    // ---- lids ------------------------------------------------------------------------------------------
    if (target.ambient !== 'none' && t >= this.nextBlink) {
      this.blinkAt = t;
      this.doubleAt = r() < 0.25 ? t + 0.3 : -10;
      this.nextBlink = t + 1.8 + r() * 2.8;
    }
    const blink =
      target.ambient === 'none' ? 0 : Math.max(blinkCurve(t - this.blinkAt), blinkCurve(t - this.doubleAt));
    const lid = clamp(
      Math.max(c.lid, blink, c.asleep, yawn * 0.9, 0.18 * Math.max(0, this.gaze[1])) * (1 - 0.8 * startle),
    );
    const lower = clamp(Math.max(c.lower, 0.5 * yawn, 0.25 * env0));

    skel.update(dt);
    const hw = skel.world[skel.index.get('head')!];
    this.anchor[0] = applyX(hw, this.def.lookAnchor[0], this.def.lookAnchor[1]);
    this.anchor[1] = applyY(hw, this.def.lookAnchor[0], this.def.lookAnchor[1]);

    return {
      iris: [
        clamp(this.gaze[0] - 0.55 * hx, -1, 1) * 3.2,
        clamp(this.gaze[1] - 0.55 * this.head[1], -1, 1) * 2.4,
      ],
      lid,
      lower,
      mouth,
      yaw: hx,
      pitch: hy,
      bodyYaw: 0.15 * hx + 0.12 * Math.sin(2 * Math.PI * this.bouncePhase * 0.5) * air,
      sleep: c.asleep,
      lift,
      light: c.light,
    };
  }

  private ease(target: ReliefTargets, dt: number): ReliefTargets {
    if (!this.cur || dt === 0) {
      this.cur = { ...target, gaze: [...target.gaze] };
      return this.cur;
    }
    const c = this.cur;
    for (const k of CHANNELS) c[k] += (target[k] - c[k]) * approach(dt, TAU[k]);
    c.gaze = target.gaze;
    c.ambient = target.ambient;
    return c;
  }
}
