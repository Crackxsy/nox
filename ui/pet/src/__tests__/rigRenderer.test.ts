/**
 * Choosing a drawing backend, and saying so when it is not the good one.
 *
 * jsdom has neither WebGL nor a real 2D context, which makes it exactly the environment this code
 * has to survive: the pet runs in whatever browser the desktop shell happens to embed, and the
 * rule is that a missing capability is reported and worked around, never assumed away.
 */

import { describe, expect, it, vi } from 'vitest';

import { createRigRenderer, RigRendererError } from '../rig/renderer';

/** A canvas whose `getContext` answers exactly what a test wants it to. */
function canvasWith(contexts: Record<string, unknown>): HTMLCanvasElement {
  const canvas = document.createElement('canvas');
  vi.spyOn(canvas, 'getContext').mockImplementation(((kind: string) => contexts[kind] ?? null) as never);
  return canvas;
}

const CANVAS_2D_STUB = {
  setTransform: vi.fn(),
  clearRect: vi.fn(),
  save: vi.fn(),
  restore: vi.fn(),
  beginPath: vi.fn(),
  moveTo: vi.fn(),
  lineTo: vi.fn(),
  closePath: vi.fn(),
  clip: vi.fn(),
  drawImage: vi.fn(),
  globalAlpha: 1,
};

describe('picking a renderer backend', () => {
  it('falls back to Canvas 2D when WebGL is not there, and says why', () => {
    const renderer = createRigRenderer(canvasWith({ '2d': CANVAS_2D_STUB }));
    expect(renderer.backend).toBe('canvas2d');
    expect(renderer.downgradeReason).toMatch(/WebGL/);
  });

  it('reports the failure rather than a bare "unavailable" when WebGL throws', () => {
    const canvas = document.createElement('canvas');
    vi.spyOn(canvas, 'getContext').mockImplementation(((kind: string) => {
      if (kind === '2d') return CANVAS_2D_STUB;
      throw new Error('context lost');
    }) as never);
    const renderer = createRigRenderer(canvas);
    expect(renderer.backend).toBe('canvas2d');
    expect(renderer.downgradeReason).toMatch(/context lost/);
  });

  it('refuses outright when neither backend exists, so the pet can fall back to frames', () => {
    expect(() => createRigRenderer(canvasWith({}))).toThrow(RigRendererError);
  });

  it('advertises a triangle budget the 2D path can actually afford', () => {
    const renderer = createRigRenderer(canvasWith({ '2d': CANVAS_2D_STUB }));
    expect(renderer.triangleBudget).toBeGreaterThan(0);
    expect(renderer.triangleBudget).toBeLessThan(1000);
  });

  it('sizes the backing store by the device pixel ratio and the box by CSS pixels', () => {
    const canvas = canvasWith({ '2d': CANVAS_2D_STUB });
    createRigRenderer(canvas).resize(260, 2);
    expect(canvas.width).toBe(520);
    expect(canvas.height).toBe(520);
    expect(canvas.style.width).toBe('260px');
  });

  it('draws one clipped, transformed triangle per triangle on the 2D path', () => {
    const context = { ...CANVAS_2D_STUB, drawImage: vi.fn(), clip: vi.fn() };
    const renderer = createRigRenderer(canvasWith({ '2d': context }));
    renderer.resize(100, 1);
    renderer.draw([
      {
        image: { width: 10, height: 10 } as unknown as TexImageSource,
        positions: new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]),
        uv: new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]),
        indices: new Uint16Array([0, 1, 2, 1, 3, 2]),
        alpha: 1,
      },
    ]);
    expect(context.drawImage).toHaveBeenCalledTimes(2);
    expect(context.clip).toHaveBeenCalledTimes(2);
  });

  it('skips a fully transparent draw call instead of paying for it', () => {
    const context = { ...CANVAS_2D_STUB, drawImage: vi.fn() };
    const renderer = createRigRenderer(canvasWith({ '2d': context }));
    renderer.resize(100, 1);
    renderer.draw([
      {
        image: { width: 10, height: 10 } as unknown as TexImageSource,
        positions: new Float32Array([0, 0, 1, 0, 0, 1]),
        uv: new Float32Array([0, 0, 1, 0, 0, 1]),
        indices: new Uint16Array([0, 1, 2]),
        alpha: 0,
      },
    ]);
    expect(context.drawImage).not.toHaveBeenCalled();
  });
});
