import { describe, expect, it } from 'vitest';

import { __drawForTests as draw } from '../Pet';
import { DEFAULT_MOOD, EXPRESSIONS, type Functional, type PetInput, deriveAnim } from '../petState';
import { VARIANTS } from '../variants';

/** Minimal duck-typed CanvasRenderingContext2D: records nothing, just must not throw when every
 * method the renderer calls is present. This is the "canvas mock" the task allows in place of a
 * full jsdom + real <canvas> — it runs in vitest's plain `node` environment (see vite.config.ts),
 * no DOM needed, and still proves every variant renders every expression without throwing. */
function fakeCtx(): CanvasRenderingContext2D {
  const noop = () => undefined;
  const gradient = { addColorStop: noop };
  const ctx: Record<string, unknown> = {
    save: noop, restore: noop, translate: noop, rotate: noop, scale: noop, setTransform: noop,
    beginPath: noop, closePath: noop, fill: noop, stroke: noop, clearRect: noop,
    moveTo: noop, lineTo: noop, arc: noop, ellipse: noop, quadraticCurveTo: noop,
    bezierCurveTo: noop, roundRect: noop, setLineDash: noop,
    createRadialGradient: () => gradient,
    measureText: () => ({ width: 10 }),
    fillText: noop,
  };
  return ctx as unknown as CanvasRenderingContext2D;
}

const FUNCTIONALS: Functional[] = [
  'idle', 'listening', 'thinking', 'speaking', 'working', 'error', 'muted', 'privacy', 'unavailable',
];

const baseInput: PetInput = {
  connected: true,
  functional: 'idle',
  expression: 'normal',
  intensity: 0.8,
  mood: DEFAULT_MOOD,
  sleep: 'none',
  speakingLevel: 0,
};

describe('Pet renderer', () => {
  it('draws every variant for every one of the 21 expressions without throwing', () => {
    expect(EXPRESSIONS.length).toBe(21);
    for (const variant of Object.values(VARIANTS)) {
      for (const expression of EXPRESSIONS) {
        const input: PetInput = { ...baseInput, expression };
        const palette = {
          hue: variant.palette.bodyHue,
          sat: variant.palette.bodySat,
          light: variant.palette.bodyLightOnDark,
        };
        const params = deriveAnim(input, palette);
        expect(() => draw(fakeCtx(), params, variant, 1.23, 512, 0, 0)).not.toThrow();
      }
    }
  });

  it('draws every variant for every functional state, including speaking and privacy, without throwing', () => {
    for (const variant of Object.values(VARIANTS)) {
      for (const functional of FUNCTIONALS) {
        const input: PetInput = { ...baseInput, functional, speakingLevel: functional === 'speaking' ? 0.7 : 0 };
        const params = deriveAnim(input);
        expect(() => draw(fakeCtx(), params, variant, 4.5, 260, params.mouthOpen, 0.5)).not.toThrow();
      }
    }
  });
});
