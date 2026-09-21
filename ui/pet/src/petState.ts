/**
 * Pet state machine (Component Model: functional × expression × mood → animation parameters).
 * Pure functions only; the renderer never decides anything (D226: state separated from rendering).
 *
 * Product decisions applied: D34 (21 expressions emerge from parameters with intensity; colour is
 * supporting, never the only signal), FR-4.13 / A390 (60 fps, 30 fallback, 10–30 or paused when
 * throttled), A146 (sleep tiers keep light animation running).
 */

export type Functional =
  | 'idle' | 'listening' | 'thinking' | 'speaking' | 'working'
  | 'error' | 'muted' | 'privacy' | 'unavailable';

export type SleepTier = 'none' | 'normal' | 'high_load' | 'offline';

export const EXPRESSIONS = [
  'normal', 'happy', 'excited', 'confused', 'curious', 'bored', 'proud', 'shy', 'scared', 'angry',
  'sad', 'coding', 'working', 'thinking', 'coaching', 'streaming', 'celebrating', 'sleeping',
  'tilted', 'hype', 'smug',
] as const;
export type Expression = (typeof EXPRESSIONS)[number];

export interface Mood {
  mood: number;
  energy: number;
  stress: number;
  curiosity: number;
  affection: number;
  attention: number;
}

export const DEFAULT_MOOD: Mood = {
  mood: 0.6, energy: 0.6, stress: 0.2, curiosity: 0.5, affection: 0.5, attention: 0.5,
};

export interface CaptureFlags {
  microphone: boolean;
  camera: boolean;
  screen: boolean;
  cloud: boolean;
}

export interface PetInput {
  connected: boolean;
  functional: Functional;
  expression: Expression | string;
  intensity: number;
  mood: Mood;
  sleep: SleepTier;
  /** 0..1, bumped by tts.chunk and decayed per frame. */
  speakingLevel: number;
}

export type Ring = 'none' | 'pulse' | 'orbit' | 'segments' | 'shield' | 'cross' | 'alert' | 'bar';

export interface AnimParams {
  fps: number;
  breathRate: number;
  breathAmp: number;
  bodyScale: number;
  tilt: number;
  bob: number;
  jitter: number;
  eyeOpen: number;
  eyeSpread: number;
  eyeLift: number;
  pupilSize: number;
  browAngle: number;
  mouthOpen: number;
  mouthCurve: number;
  hue: number;
  sat: number;
  light: number;
  glow: number;
  ring: Ring;
  label: string | null;
  /**
   * Skip drawing entirely (FR-4.13/A390 "… or paused when throttled"). `deriveAnim` never sets it
   * — no core state means "stop animating" — but `App.tsx` does for `?still=1`, where exactly one
   * frame is wanted, and `Pet.tsx` honours it in its animation loop.
   */
  paused: boolean;
}

export const BASE_HUE = 255; // violet, the neutral placeholder tint (creature design is OP-A)

/** Base tint fed into deriveAnim; comes from the active PetVariant's palette (OP-1). Expression
 * deltas (hueShift below) shift away from this, they never replace it (D34: colour only supports). */
export interface BasePalette {
  hue: number;
  sat: number;
  light: number;
}

export const NEUTRAL_PALETTE: BasePalette = { hue: BASE_HUE, sat: 0.62, light: 0.68 };

interface ExpressionDelta {
  eyeOpen?: number;
  mouthCurve?: number;
  tilt?: number;
  /** Relative hue nudge in degrees, scaled by intensity. Use for "a little warmer/cooler". */
  hueShift?: number;
  /**
   * Absolute hue in degrees the expression pulls *towards*, blended by intensity along the shorter
   * way round the colour wheel. Use whenever the expression means a specific colour ("angry is
   * red"), because a relative shift only lands on that colour for one body hue: `-255°` from the
   * neutral 255° violet is red, but from the imp's 320° it is yellow-green and from the cat's 276°
   * it is orange. Alarm states have to look the same on every variant (D238 — never colour alone,
   * but when colour speaks it must not lie).
   */
  hueTarget?: number;
  energy?: number;
  brow?: number;
  eyeLift?: number;
  pupil?: number;
  glow?: number;
  bob?: number;
}

/** Blend `from` towards `to` by `k` (0..1) along the shorter arc of the hue circle. */
export function blendHue(from: number, to: number, k: number): number {
  const delta = (((to - from) % 360) + 540) % 360 - 180;
  return (((from + delta * clamp(k)) % 360) + 360) % 360;
}

/** Per-expression deltas at full intensity. Mimicry/posture first; hue only as support (D34). */
export const EXPRESSION_TABLE: Record<Expression, ExpressionDelta> = {
  normal: {},
  happy: { mouthCurve: 0.7, eyeOpen: -0.1, glow: 0.2, bob: 0.3 },
  excited: { mouthCurve: 0.9, eyeOpen: 0.25, energy: 0.6, glow: 0.4, bob: 0.8, pupil: 0.3 },
  confused: { tilt: 0.25, eyeOpen: 0.1, brow: -0.4, mouthCurve: -0.1 },
  curious: { tilt: 0.15, eyeLift: 0.3, pupil: 0.4, eyeOpen: 0.15 },
  bored: { eyeOpen: -0.5, mouthCurve: -0.2, energy: -0.5, tilt: -0.05 },
  proud: { mouthCurve: 0.5, eyeLift: 0.2, eyeOpen: -0.2, glow: 0.3 },
  shy: { tilt: -0.2, eyeOpen: -0.3, mouthCurve: 0.2, hueShift: 30, eyeLift: -0.2 },
  scared: { eyeOpen: 0.4, mouthCurve: -0.5, energy: 0.4, pupil: -0.4, hueShift: -20 },
  angry: { brow: 0.9, mouthCurve: -0.6, eyeOpen: -0.2, hueTarget: 2, energy: 0.3 },
  sad: { mouthCurve: -0.8, eyeOpen: -0.35, eyeLift: -0.3, hueShift: -40, energy: -0.4 },
  coding: { eyeOpen: -0.15, pupil: 0.2, mouthCurve: 0.1, energy: 0.1 },
  working: { eyeOpen: -0.1, mouthCurve: 0.0, energy: 0.2 },
  thinking: { eyeLift: 0.4, tilt: 0.1, mouthCurve: -0.05 },
  coaching: { eyeOpen: 0.1, mouthCurve: 0.3, brow: 0.2, energy: 0.3 },
  streaming: { mouthCurve: 0.5, eyeOpen: 0.1, glow: 0.4, energy: 0.3 },
  celebrating: { mouthCurve: 1.0, eyeOpen: 0.2, glow: 0.7, bob: 1.0, energy: 0.7 },
  sleeping: { eyeOpen: -1.0, mouthCurve: 0.1, energy: -0.9 },
  tilted: { tilt: 0.45, brow: 0.6, mouthCurve: -0.4, hueTarget: 22, energy: 0.2 },
  hype: { mouthCurve: 0.9, eyeOpen: 0.3, glow: 0.8, bob: 1.0, energy: 0.9, hueShift: 40 },
  smug: { mouthCurve: 0.4, eyeOpen: -0.45, tilt: -0.1, brow: 0.3 },
};

export function isExpression(x: string): x is Expression {
  return (EXPRESSIONS as readonly string[]).includes(x);
}

export const clamp = (x: number, lo = 0, hi = 1): number => Math.min(hi, Math.max(lo, x));

/** Frame-rate policy: sleep tiers and unavailability reduce work (A146, FR-4.13). */
export function fpsFor(sleep: SleepTier, functional: Functional, connected: boolean): number {
  if (!connected) return 10;
  if (functional === 'unavailable') return 10;
  switch (sleep) {
    case 'none':
      return 60;
    case 'normal':
      return 30;
    case 'high_load':
      return 15;
    case 'offline':
      return 20;
  }
}

/** tts.chunk arrived: pulse to a level that depends on chunk size. */
export function bumpSpeaking(level: number, chunkChars = 20): number {
  return clamp(Math.max(level, 0.55 + Math.min(chunkChars, 60) / 150));
}

/** Exponential decay of the speaking pulse (~180 ms time constant). */
export function decaySpeaking(level: number, dtMs: number): number {
  const next = level * Math.exp(-dtMs / 180);
  return next < 0.02 ? 0 : next;
}

export function deriveAnim(input: PetInput, palette: BasePalette = NEUTRAL_PALETTE): AnimParams {
  const intensity = clamp(input.intensity);
  const mood = input.mood;
  const delta = isExpression(input.expression) ? EXPRESSION_TABLE[input.expression] : {};
  const d = (v: number | undefined) => (v ?? 0) * intensity;

  const energy = clamp(mood.energy + d(delta.energy));
  const p: AnimParams = {
    fps: fpsFor(input.sleep, input.functional, input.connected),
    breathRate: 0.22 + energy * 0.28,
    breathAmp: 0.03 + energy * 0.03,
    bodyScale: 1,
    tilt: d(delta.tilt),
    bob: d(delta.bob) * energy,
    jitter: clamp(mood.stress - 0.5) * 2,
    eyeOpen: clamp(0.85 + d(delta.eyeOpen), 0.02, 1),
    eyeSpread: 1,
    eyeLift: d(delta.eyeLift),
    pupilSize: clamp(0.5 + d(delta.pupil) + (mood.attention - 0.5) * 0.3, 0.2, 1),
    browAngle: d(delta.brow),
    mouthOpen: 0,
    mouthCurve: clamp((mood.mood - 0.5) * 0.6 + d(delta.mouthCurve), -1, 1),
    hue:
      delta.hueTarget === undefined
        ? (palette.hue + d(delta.hueShift) + 360) % 360
        : blendHue(palette.hue, delta.hueTarget, intensity),
    sat: palette.sat,
    light: palette.light,
    glow: clamp(0.15 + d(delta.glow) + (mood.affection - 0.5) * 0.2),
    ring: 'none',
    label: null,
    paused: false,
  };

  // Functional state is the primary, always-distinguishable layer.
  switch (input.functional) {
    case 'idle':
      break;
    case 'listening':
      p.ring = 'pulse';
      p.eyeOpen = clamp(p.eyeOpen + 0.15);
      p.pupilSize = clamp(p.pupilSize + 0.2);
      break;
    case 'thinking':
      p.ring = 'orbit';
      p.eyeLift = Math.max(p.eyeLift, 0.35);
      p.mouthCurve = Math.min(p.mouthCurve, 0.1);
      break;
    case 'speaking':
      p.mouthOpen = clamp(input.speakingLevel);
      p.glow = clamp(p.glow + input.speakingLevel * 0.3);
      break;
    case 'working':
      p.ring = 'segments';
      p.eyeOpen = Math.min(p.eyeOpen, 0.6);
      break;
    case 'error':
      p.ring = 'alert';
      p.hue = 8;
      p.tilt = p.tilt || 0.2;
      p.mouthCurve = Math.min(p.mouthCurve, -0.3);
      p.label = 'error';
      break;
    case 'muted':
      p.ring = 'cross';
      p.mouthOpen = 0;
      p.mouthCurve = 0;
      p.sat = 0.35;
      break;
    case 'privacy':
      p.ring = 'shield';
      p.eyeOpen = 0.08;
      p.sat = 0.3;
      p.light = 0.55;
      p.label = 'privacy';
      break;
    case 'unavailable':
      p.ring = 'bar';
      p.eyeOpen = 0.05;
      p.sat = 0.05;
      p.light = 0.45;
      p.glow = 0;
      p.breathRate = 0.12;
      p.label = 'unavailable';
      break;
  }

  if (input.sleep === 'offline' && input.functional === 'idle') {
    p.label = 'offline mode';
  }

  // Honest offline state: no connection means we do not know anything about Nox.
  if (!input.connected) {
    p.ring = 'bar';
    p.eyeOpen = 0.05;
    p.mouthOpen = 0;
    p.mouthCurve = 0;
    p.sat = 0;
    p.light = 0.4;
    p.glow = 0;
    p.tilt = 0;
    p.bob = 0;
    p.jitter = 0;
    p.breathRate = 0.1;
    p.breathAmp = 0.015;
    p.label = 'offline';
  }
  return p;
}

// ---- event reducer -------------------------------------------------------------------------------

export interface PetState {
  connected: boolean;
  functional: Functional;
  expression: string;
  intensity: number;
  mood: Mood;
  sleep: SleepTier;
  speakingLevel: number;
  capture: CaptureFlags;
  privacyMode: string;
  muted: boolean;
}

export const INITIAL_STATE: PetState = {
  connected: false,
  functional: 'idle',
  expression: 'normal',
  intensity: 0.5,
  mood: DEFAULT_MOOD,
  sleep: 'none',
  speakingLevel: 0,
  capture: { microphone: false, camera: false, screen: false, cloud: false },
  privacyMode: 'balanced',
  muted: false,
};

const FUNCTIONALS: ReadonlySet<string> = new Set([
  'idle', 'listening', 'thinking', 'speaking', 'working', 'error', 'muted', 'privacy', 'unavailable',
]);
const SLEEPS: ReadonlySet<string> = new Set(['none', 'normal', 'high_load', 'offline']);

function num(x: unknown, fallback: number): number {
  return typeof x === 'number' && Number.isFinite(x) ? x : fallback;
}

/** Apply one IPC event by name/payload. Unknown events leave the state untouched. */
export function reduceEvent(state: PetState, name: string, payload: Record<string, unknown>): PetState {
  switch (name) {
    case 'pet.state_changed': {
      const f = String(payload.functional ?? state.functional);
      const moodRaw = (payload.mood ?? {}) as Record<string, unknown>;
      const mood: Mood = {
        mood: num(moodRaw.mood, state.mood.mood),
        energy: num(moodRaw.energy, state.mood.energy),
        stress: num(moodRaw.stress, state.mood.stress),
        curiosity: num(moodRaw.curiosity, state.mood.curiosity),
        affection: num(moodRaw.affection, state.mood.affection),
        attention: num(moodRaw.attention, state.mood.attention),
      };
      return {
        ...state,
        functional: FUNCTIONALS.has(f) ? (f as Functional) : state.functional,
        expression: String(payload.expression ?? state.expression),
        intensity: clamp(num(payload.intensity, state.intensity)),
        mood,
        speakingLevel: f === 'speaking' ? state.speakingLevel : 0,
      };
    }
    case 'tts.started':
      return { ...state, speakingLevel: Math.max(state.speakingLevel, 0.4) };
    case 'tts.chunk': {
      const text = typeof payload.text === 'string' ? payload.text : '';
      return { ...state, speakingLevel: bumpSpeaking(state.speakingLevel, text.length || 20) };
    }
    case 'tts.finished':
    case 'tts.interrupted':
      return { ...state, speakingLevel: 0 };
    case 'privacy.capture_changed':
      return {
        ...state,
        capture: {
          microphone: payload.microphone === true,
          camera: payload.camera === true,
          screen: payload.screen === true,
          cloud: payload.cloud === true,
        },
      };
    case 'privacy.mode_changed':
      return { ...state, privacyMode: String(payload.current ?? state.privacyMode) };
    case 'voice.muted':
      return { ...state, muted: payload.muted !== false };
    case 'system.stopping':
      return { ...state, functional: 'unavailable', speakingLevel: 0 };
    case 'state.changed': {
      const path = String(payload.path ?? '');
      if (path === 'assistant.sleep' && typeof payload.new === 'string' && SLEEPS.has(payload.new)) {
        return { ...state, sleep: payload.new as SleepTier };
      }
      if (path === 'assistant.muted') return { ...state, muted: payload.new === true };
      return state;
    }
    default:
      return state;
  }
}

// ---- still-frame dev override (render_variants.py, OP-1) -----------------------------------------

/** The 6 representative states rendered per variant for the OP-1 gallery (Decision Plan
 * 2026-09-11): a mix of `functional` (speaking, privacy) and `expression` (normal, happy, thinking,
 * sleeping) states, matched to what most clearly shows the variant's silhouette and motion. */
export const STILL_EXPRESSIONS = ['normal', 'happy', 'thinking', 'speaking', 'privacy', 'sleeping'] as const;
export type StillExpressionName = (typeof STILL_EXPRESSIONS)[number];

const STILL_PRESETS: Record<StillExpressionName, { functional: Functional; expression: Expression }> = {
  normal: { functional: 'idle', expression: 'normal' },
  happy: { functional: 'idle', expression: 'happy' },
  thinking: { functional: 'thinking', expression: 'thinking' },
  speaking: { functional: 'speaking', expression: 'normal' },
  privacy: { functional: 'privacy', expression: 'normal' },
  sleeping: { functional: 'idle', expression: 'sleeping' },
};

export function isStillExpressionName(x: string): x is StillExpressionName {
  return (STILL_EXPRESSIONS as readonly string[]).includes(x);
}

/** Build a static PetState for `?still=1&expression=<name>` (dev-only, no WebSocket): renders one
 * frame of a named state so `render_variants.py` can screenshot it headlessly. Unknown names fall
 * back to `normal`. */
export function stillState(name: string | null): PetState {
  const preset = name && isStillExpressionName(name) ? STILL_PRESETS[name] : STILL_PRESETS.normal;
  return {
    ...INITIAL_STATE,
    connected: true,
    functional: preset.functional,
    expression: preset.expression,
    intensity: 0.85,
    speakingLevel: preset.functional === 'speaking' ? 0.8 : 0,
  };
}

export function toInput(state: PetState): PetInput {
  return {
    connected: state.connected,
    functional: state.functional,
    expression: state.expression,
    intensity: state.intensity,
    mood: state.mood,
    sleep: state.sleep,
    speakingLevel: state.speakingLevel,
  };
}
