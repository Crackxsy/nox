import { describe, expect, it } from 'vitest';

import {
  DEFAULT_MOOD,
  EXPRESSIONS,
  EXPRESSION_TABLE,
  INITIAL_STATE,
  NEUTRAL_PALETTE,
  STILL_EXPRESSIONS,
  type Functional,
  type PetInput,
  bumpSpeaking,
  decaySpeaking,
  deriveAnim,
  fpsFor,
  isStillExpressionName,
  reduceEvent,
  stillState,
  toInput,
} from '../petState';

const base: PetInput = {
  connected: true,
  functional: 'idle',
  expression: 'normal',
  intensity: 0.5,
  mood: DEFAULT_MOOD,
  sleep: 'none',
  speakingLevel: 0,
};

const FUNCTIONALS: Functional[] = [
  'idle', 'listening', 'thinking', 'speaking', 'working', 'error', 'muted', 'privacy', 'unavailable',
];

describe('fps policy', () => {
  it('reduces frame rate per sleep tier and when unavailable/offline', () => {
    expect(fpsFor('none', 'idle', true)).toBe(60);
    expect(fpsFor('normal', 'idle', true)).toBe(30);
    expect(fpsFor('high_load', 'idle', true)).toBe(15);
    expect(fpsFor('offline', 'idle', true)).toBe(20);
    expect(fpsFor('none', 'unavailable', true)).toBe(10);
    expect(fpsFor('none', 'idle', false)).toBe(10);
  });
});

describe('deriveAnim', () => {
  it('produces a distinguishable visual per functional state', () => {
    const seen = new Set<string>();
    for (const f of FUNCTIONALS) {
      const p = deriveAnim({ ...base, functional: f, speakingLevel: f === 'speaking' ? 0.8 : 0 });
      const signature = `${p.ring}|${p.mouthOpen > 0.1}|${p.eyeOpen < 0.2}|${p.hue < 30}|${p.label}`;
      expect(seen.has(signature), `duplicate visual for ${f}: ${signature}`).toBe(false);
      seen.add(signature);
    }
  });
  it('honest offline state overrides everything', () => {
    const p = deriveAnim({ ...base, connected: false, functional: 'speaking', speakingLevel: 1, expression: 'hype', intensity: 1 });
    expect(p.label).toBe('offline');
    expect(p.mouthOpen).toBe(0);
    expect(p.sat).toBe(0);
    expect(p.ring).toBe('bar');
    expect(p.fps).toBe(10);
  });
  it('speaking opens the mouth with the tts level', () => {
    expect(deriveAnim({ ...base, functional: 'speaking', speakingLevel: 0.7 }).mouthOpen).toBeCloseTo(0.7);
    expect(deriveAnim({ ...base, functional: 'idle', speakingLevel: 0.7 }).mouthOpen).toBe(0);
  });
  it('all 21 expressions are known and intensity scales the delta', () => {
    expect(EXPRESSIONS.length).toBe(21);
    for (const e of EXPRESSIONS) expect(EXPRESSION_TABLE[e]).toBeDefined();
    const low = deriveAnim({ ...base, expression: 'angry', intensity: 0.2 });
    const high = deriveAnim({ ...base, expression: 'angry', intensity: 1 });
    expect(high.browAngle).toBeGreaterThan(low.browAngle);
    expect(high.mouthCurve).toBeLessThan(low.mouthCurve);
    // colour supports (red-ish for angry at full intensity) but posture changed too
    expect(high.hue).toBeLessThan(30);
    expect(deriveAnim({ ...base, expression: 'sleeping', intensity: 1 }).eyeOpen).toBeLessThan(0.1);
  });
  it('unknown expressions fall back to neutral', () => {
    const p = deriveAnim({ ...base, expression: 'nonsense' });
    expect(p).toEqual(deriveAnim({ ...base, expression: 'normal' }));
  });
  it('mood modulates breathing and mouth', () => {
    const tired = deriveAnim({ ...base, mood: { ...DEFAULT_MOOD, energy: 0.1, mood: 0.1 } });
    const lively = deriveAnim({ ...base, mood: { ...DEFAULT_MOOD, energy: 0.9, mood: 0.9 } });
    expect(lively.breathRate).toBeGreaterThan(tired.breathRate);
    expect(lively.mouthCurve).toBeGreaterThan(tired.mouthCurve);
  });
  it('an explicit palette shifts the base hue/sat/light but leaves posture untouched', () => {
    const neutral = deriveAnim({ ...base, expression: 'happy' });
    const tinted = deriveAnim({ ...base, expression: 'happy' }, { hue: 30, sat: 0.5, light: 0.5 });
    expect(tinted.hue).not.toBe(neutral.hue);
    expect(tinted.mouthCurve).toBe(neutral.mouthCurve);
    expect(tinted.eyeOpen).toBe(neutral.eyeOpen);
    expect(deriveAnim({ ...base }, NEUTRAL_PALETTE)).toEqual(deriveAnim({ ...base }));
  });
});

describe('still-frame dev override (render_variants.py)', () => {
  it('recognises exactly the 6 representative names', () => {
    expect(STILL_EXPRESSIONS.length).toBe(6);
    for (const name of STILL_EXPRESSIONS) expect(isStillExpressionName(name)).toBe(true);
    expect(isStillExpressionName('nonsense')).toBe(false);
  });
  it('builds a connected, non-zero-intensity state for each name and falls back to normal', () => {
    for (const name of STILL_EXPRESSIONS) {
      const s = stillState(name);
      expect(s.connected).toBe(true);
      expect(s.intensity).toBeGreaterThan(0);
    }
    expect(stillState('speaking').functional).toBe('speaking');
    expect(stillState('speaking').speakingLevel).toBeGreaterThan(0);
    expect(stillState('privacy').functional).toBe('privacy');
    expect(stillState('sleeping').expression).toBe('sleeping');
    expect(stillState(null)).toEqual(stillState('normal'));
    expect(stillState('nonsense')).toEqual(stillState('normal'));
  });
});

describe('speaking pulse', () => {
  it('bumps and decays', () => {
    const l = bumpSpeaking(0, 30);
    expect(l).toBeGreaterThan(0.5);
    expect(bumpSpeaking(0.9, 5)).toBe(0.9);
    expect(decaySpeaking(l, 100)).toBeLessThan(l);
    expect(decaySpeaking(0.01, 100)).toBe(0);
  });
});

describe('reduceEvent', () => {
  it('applies pet.state_changed with validation', () => {
    const s = reduceEvent(INITIAL_STATE, 'pet.state_changed', {
      functional: 'thinking', expression: 'curious', intensity: 0.8, mood: { energy: 0.9, bogus: 'x' },
    });
    expect(s.functional).toBe('thinking');
    expect(s.expression).toBe('curious');
    expect(s.intensity).toBe(0.8);
    expect(s.mood.energy).toBe(0.9);
    expect(s.mood.stress).toBe(DEFAULT_MOOD.stress);
    const bad = reduceEvent(s, 'pet.state_changed', { functional: 'exploded', intensity: 7 });
    expect(bad.functional).toBe('thinking');
    expect(bad.intensity).toBe(1);
  });
  it('tts events drive the speaking level, capture and privacy update, sleep from state.changed', () => {
    let s = reduceEvent(INITIAL_STATE, 'pet.state_changed', { functional: 'speaking' });
    s = reduceEvent(s, 'tts.chunk', { text: 'Hallo Alex' });
    expect(s.speakingLevel).toBeGreaterThan(0.5);
    s = reduceEvent(s, 'tts.finished', {});
    expect(s.speakingLevel).toBe(0);
    s = reduceEvent(s, 'privacy.capture_changed', { microphone: true, camera: false, screen: 'yes', cloud: false });
    expect(s.capture).toEqual({ microphone: true, camera: false, screen: false, cloud: false });
    s = reduceEvent(s, 'privacy.mode_changed', { previous: 'balanced', current: 'private', by: 'user' });
    expect(s.privacyMode).toBe('private');
    s = reduceEvent(s, 'state.changed', { path: 'assistant.sleep', old: 'none', new: 'high_load', version: 2 });
    expect(s.sleep).toBe('high_load');
    s = reduceEvent(s, 'state.changed', { path: 'assistant.sleep', old: 'none', new: 'nap', version: 3 });
    expect(s.sleep).toBe('high_load');
    s = reduceEvent(s, 'system.stopping', {});
    expect(s.functional).toBe('unavailable');
    expect(reduceEvent(s, 'weird.event', { a: 1 })).toBe(s);
    expect(toInput(s).functional).toBe('unavailable');
  });
});
