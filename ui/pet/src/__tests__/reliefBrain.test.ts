/**
 * The relief creature over time, on the real eulenfuchs rig: state changes are movements (never a
 * jump), it blinks and looks around on its own, the jaw talks only while there is a voice, and
 * switching to a key pose is hidden under a squash.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { DEFAULT_MOOD, type PetInput, deriveAnim } from '../petState';
import { type Face, ReliefBrain, hopShape } from '../relief/brain';
import { parseReliefDefinition } from '../relief/reliefFile';
import { Skeleton } from '../relief/skeleton';
import { FULL_OPEN_RMS, SpeechEnvelope, VOICE_HOLD_MS, VoiceTracker } from '../relief/speech';
import { reliefTargets } from '../relief/stateMapping';
import { PoseSwitch, SWITCH_MS, SWITCH_REDUCED_MS, switchMix, switchSquash } from '../relief/transition';

const RAW: unknown = JSON.parse(
  readFileSync(join(process.cwd(), 'public', 'variants', 'eulenfuchs', 'relief', 'relief.json'), 'utf-8'),
);
const DEF = parseReliefDefinition(RAW, 'eulenfuchs');
const DT = 1 / 60;

function input(overrides: Partial<PetInput> = {}): PetInput {
  return {
    connected: true,
    functional: 'idle',
    expression: 'normal',
    intensity: 1,
    mood: DEFAULT_MOOD,
    sleep: 'none',
    speakingLevel: 0,
    feeds: 0,
    ...overrides,
  };
}

/** Run the brain through a list of [seconds, state] segments and collect what happened. */
function run(segments: [number, PetInput][], reduced = false) {
  const skel = new Skeleton(DEF.bones);
  const brain = new ReliefBrain(DEF, 42);
  const faces: Face[] = [];
  const headRot: number[] = [];
  const earRot: number[] = [];
  let t = 0;
  let first = true;
  for (const [seconds, state] of segments) {
    const target = reliefTargets(state, deriveAnim(state), reduced);
    for (let i = 0; i < Math.round(seconds / DT); i++) {
      faces.push(brain.update(t, first ? 0 : DT, skel, target));
      headRot.push(skel.get('head').rot);
      earRot.push(skel.get('ear_l').rot);
      first = false;
      t += DT;
    }
  }
  return { faces, headRot, earRot };
}

const maxStep = (xs: number[]): number => Math.max(...xs.slice(1).map((x, i) => Math.abs(x - xs[i])));

describe('relief brain', () => {
  it('moves into a new mood instead of jumping to it', () => {
    // reduced motion: no ear twitches, which are meant to be quick; this is about posture
    const seq: [number, PetInput][] = [
      [1, input()],
      [2, input({ expression: 'sad' })],
      [2, input({ expression: 'excited' })],
    ];
    const { earRot, headRot } = run(seq, true);
    // ears end up somewhere else entirely...
    expect(Math.abs(earRot[earRot.length - 1] - earRot[59])).toBeGreaterThan(0.1);
    // ...but no single frame moves them more than a smooth motion would
    expect(maxStep(earRot)).toBeLessThan(0.08);
    expect(maxStep(headRot)).toBeLessThan(0.05);
  });

  it('blinks on its own, but not under reduced motion', () => {
    const awake = run([[8, input()]]).faces;
    expect(awake.some((f) => f.lid > 0.9)).toBe(true);
    const calm = run([[8, input()]], true).faces;
    expect(calm.every((f) => f.lid < 0.3)).toBe(true);
  });

  it('looks around with real head turns of about 20 degrees when idle', () => {
    const { faces } = run([[30, input()]]);
    const maxYaw = Math.max(...faces.map((f) => Math.abs(f.yaw)));
    // yaw 1 is the rig's full turn (0.42 rad); half of it is ~12 degrees, 0.8 is ~19 degrees
    expect(maxYaw * DEF.turn.yaw).toBeGreaterThan((10 * Math.PI) / 180);
    expect(maxYaw).toBeLessThanOrEqual(1.2);
  });

  it('keeps still under reduced motion: no glances, no bounce', () => {
    const { faces } = run([[10, input({ expression: 'celebrating' })]], true);
    expect(Math.max(...faces.map((f) => Math.abs(f.yaw)))).toBeLessThan(0.05);
    expect(Math.max(...faces.map((f) => f.lift))).toBe(0);
  });

  it('talks only while there is a voice: the jaw opens and closes in syllables', () => {
    const { faces } = run([
      [1, input()],
      [3, input({ functional: 'speaking', speakingLevel: 0.8 })],
      [1.5, input()],
    ]);
    const quiet = faces.slice(0, 60);
    const talking = faces.slice(70, 240);
    const after = faces.slice(-30);
    expect(Math.max(...quiet.map((f) => f.mouth))).toBeLessThan(0.05);
    expect(Math.max(...talking.map((f) => f.mouth))).toBeGreaterThan(0.4);
    // syllables: the jaw closes between them, many times a second
    let closings = 0;
    for (let i = 1; i < talking.length; i++)
      if (talking[i - 1].mouth > 0.25 && talking[i].mouth <= 0.25) closings++;
    expect(closings).toBeGreaterThan(5);
    expect(Math.max(...after.map((f) => f.mouth))).toBeLessThan(0.05);
  });

  it('falls asleep with the eyes shut and the head sinking, and wakes with a yawn', () => {
    const { faces } = run([
      [1, input()],
      [4, input({ expression: 'sleeping' })],
      [2, input()],
    ]);
    const asleep = faces[60 + 4 * 60 - 1];
    expect(asleep.lid).toBeGreaterThan(0.95);
    expect(asleep.sleep).toBeGreaterThan(0.9);
    const waking = faces.slice(300, 360);
    expect(Math.max(...waking.map((f) => f.mouth))).toBeGreaterThan(0.5); // the yawn
  });

  it('bounces with squash and stretch when celebrating', () => {
    const { faces } = run([[3, input({ expression: 'celebrating' })]]);
    expect(Math.max(...faces.map((f) => f.lift))).toBeGreaterThan(0.02);
    const h = [0, 0.1, 0.4, 0.8].map(hopShape);
    expect(h[1].squash).toBeLessThan(0); // anticipation
    expect(h[2].lift).toBeGreaterThan(0.5); // airborne
    expect(h[3].squash).toBeLessThan(0); // landing
  });

  it('flinches when Nox is stopped', () => {
    const skel = new Skeleton(DEF.bones);
    const brain = new ReliefBrain(DEF, 1);
    const target = reliefTargets(input(), deriveAnim(input()), false);
    brain.update(0, 0, skel, target);
    brain.startle(0.5);
    let minSy = 1;
    for (let t = 0.5; t < 0.8; t += DT) {
      brain.update(t, DT, skel, target);
      minSy = Math.min(minSy, skel.get('root').sy);
    }
    expect(minSy).toBeLessThan(0.97);
  });
});

describe('speech envelope', () => {
  it('is silent without a voice and stays within 0..1 with one', () => {
    const env = new SpeechEnvelope(3);
    let t = 0;
    for (let i = 0; i < 60; i++, t += DT) expect(env.sample(t, DT, 0)).toBe(0);
    for (let i = 0; i < 600; i++, t += DT) {
      const v = env.sample(t, DT, 1);
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThanOrEqual(1);
    }
  });

  it('follows a measured amplitude when the core provides one', () => {
    const env = new SpeechEnvelope(3);
    let v = 0;
    for (let i = 0; i < 30; i++) v = env.sample(i * DT, DT, 0, FULL_OPEN_RMS / 2);
    expect(v).toBeCloseTo(Math.pow(0.5, 0.8), 2); // half the full-open loudness, a bit more than half open
    for (let i = 30; i < 60; i++) v = env.sample(i * DT, DT, 0, FULL_OPEN_RMS * 3);
    expect(v).toBeCloseTo(1, 2);
  });

  it('trusts a measured level only while new ones keep arriving', () => {
    const tracker = new VoiceTracker();
    expect(tracker.sample(0, 0, 0, true)).toBeNull(); // nothing has arrived since the pet started
    expect(tracker.sample(100, 1, 0.3, true)).toBeCloseTo(0.3);
    expect(tracker.sample(100 + VOICE_HOLD_MS - 1, 1, 0.3, true)).toBeCloseTo(0.3);
    // stalled (a dropped tts.finished, a lost connection): shut, never frozen open
    expect(tracker.sample(100 + VOICE_HOLD_MS + 1, 1, 0.3, true)).toBe(0);
    expect(tracker.sample(1000, 2, 0.1, true)).toBeCloseTo(0.1);
  });

  it('opens the jaw with the measured voice, and keeps it shut when muted', () => {
    const run = (state: PetInput, voice: number | null) => {
      const skel = new Skeleton(DEF.bones);
      const brain = new ReliefBrain(DEF, 3);
      const target = { ...reliefTargets(state, deriveAnim(state), false), voice };
      let mouth = 0;
      for (let i = 0; i < 30; i++) mouth = brain.update(i * DT, i ? DT : 0, skel, target).mouth;
      return mouth;
    };
    expect(run(input({ functional: 'speaking', speakingLevel: 0.5 }), FULL_OPEN_RMS)).toBeGreaterThan(0.9);
    expect(run(input({ functional: 'speaking', speakingLevel: 0.5 }), 0)).toBeLessThan(0.05);
    expect(run(input({ functional: 'muted', speakingLevel: 0.5 }), FULL_OPEN_RMS)).toBeLessThan(0.05);
  });
});

describe('pose switch', () => {
  it('changes the picture only while the creature is squashed down', () => {
    for (let k = 0; k <= 1; k += 0.01) {
      const mix = switchMix(k);
      if (mix > 0.02 && mix < 0.98) expect(switchSquash(k), `k=${k}`).toBeLessThan(-0.04);
    }
  });

  it('fades a key pose in under a squash and ends exactly on it', () => {
    const sw = new PoseSwitch();
    sw.set(null);
    const alphas: number[] = [];
    for (let ms = 0; ms <= SWITCH_MS + 20; ms += 10) alphas.push(sw.update(ms, 'curl', false).poseAlpha);
    for (let i = 1; i < alphas.length; i++) expect(alphas[i]).toBeGreaterThanOrEqual(alphas[i - 1]);
    const end = sw.update(SWITCH_MS + 40, 'curl', false);
    expect(end).toMatchObject({ pose: 'curl', poseAlpha: 1, squash: 0, reliefVisible: false, busy: false });
  });

  it('goes from one key pose to another through the creature itself', () => {
    const sw = new PoseSwitch();
    sw.set('eat');
    const mid = sw.update(0, 'curl', false);
    expect(mid.pose).toBe('eat');
    let ms = 0;
    while (sw.update(ms, 'curl', false).pose !== 'curl') ms += 10;
    expect(ms).toBeGreaterThanOrEqual(SWITCH_MS);
  });

  it('under reduced motion is a short dissolve without squash', () => {
    const sw = new PoseSwitch();
    sw.set(null);
    for (let ms = 0; ms < SWITCH_REDUCED_MS; ms += 10) expect(sw.update(ms, 'lie', true).squash).toBe(0);
    expect(sw.update(SWITCH_REDUCED_MS + 1, 'lie', true).poseAlpha).toBe(1);
  });
});
