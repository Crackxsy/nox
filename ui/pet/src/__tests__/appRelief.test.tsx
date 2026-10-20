/**
 * Which renderer the pet ends up with, end to end through `App`: the relief renderer when it can
 * run, the older rig whenever it cannot - no WebGL2, a failed or hanging load - and never a pet
 * that stays invisible. Also that a relief renderer is released when it is no longer shown.
 *
 * Loading and the GPU are stand-ins; everything between them (App, ReliefPet, the brain, the
 * geometry) is the real code.
 */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App, RELIEF_LOAD_TIMEOUT_MS } from '../App';
import { parseReliefDefinition } from '../relief/reliefFile';
import { loadRelief } from '../relief/reliefFile';
import { loadRig } from '../rig';
import { parseRigDefinition } from '../rig/rigFile';

const VARIANTS = join(process.cwd(), 'public', 'variants');
const json = (...p: string[]): unknown => JSON.parse(readFileSync(join(VARIANTS, ...p), 'utf-8'));

const gpu = vi.hoisted(() => ({ webgl2: true, created: 0, disposed: 0 }));

vi.mock('../variants/sprite', async (original) => ({
  ...(await original<typeof import('../variants/sprite')>()),
  loadSprite: vi.fn(async (id: string) => ({ manifest: { name: id }, base: '', urls: {} })),
}));
vi.mock('../rig', async (original) => ({
  ...(await original<typeof import('../rig')>()),
  loadRig: vi.fn(),
}));
vi.mock('../relief/reliefFile', async (original) => ({
  ...(await original<typeof import('../relief/reliefFile')>()),
  loadRelief: vi.fn(),
}));
vi.mock('../RiggedPet', () => ({ RiggedPet: () => <div data-testid="rigged" /> }));
// the procedural placeholder shown before the sprite set arrives
vi.mock('../Pet', () => ({ Pet: () => <div data-testid="procedural" /> }));
vi.mock('../relief/renderer', () => ({
  ReliefRenderer: class {
    contextLost = false;
    constructor(canvas: HTMLCanvasElement) {
      if (!canvas.getContext('webgl2')) throw new Error('WebGL2 is not available');
      gpu.created += 1;
    }
    resize(): void {}
    frame(): void {}
    dispose(): void {
      gpu.disposed += 1;
    }
  },
}));

const image = { width: 1024, height: 1024 } as unknown as TexImageSource;

function rigFor(id: string) {
  return { definition: parseRigDefinition(json(id, 'rig.json'), id), images: {}, missingPoses: [] };
}

beforeEach(() => {
  gpu.webgl2 = true;
  gpu.created = 0;
  gpu.disposed = 0;
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'info').mockImplementation(() => {});
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (type: string) {
    if (type === 'webgl2') return gpu.webgl2 ? ({} as WebGL2RenderingContext) : null;
    if (type === '2d') {
      return {
        drawImage: () => {},
        getImageData: (_x: number, _y: number, w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4) }),
      } as unknown as CanvasRenderingContext2D;
    }
    return null;
  } as typeof HTMLCanvasElement.prototype.getContext);
  vi.mocked(loadRig).mockImplementation(async (id: string) => rigFor(id));
  vi.mocked(loadRelief).mockImplementation(async (id: string) => ({
    definition: parseReliefDefinition(json(id, 'relief', 'relief.json'), id),
    atlas: image,
    depth: image,
  }));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const app = (variant: string) => (
  <App
    token={null}
    lang="en"
    overlay={false}
    variantId={variant}
    still={false}
    animate={false}
    stillExpression={null}
  />
);

describe('App renderer choice', () => {
  it('draws the eulenfuchs with the relief renderer and releases it on unmount', async () => {
    const { unmount } = render(app('sprite:eulenfuchs'));
    await waitFor(() => expect(gpu.created).toBe(1));
    expect(screen.queryByTestId('rigged')).toBeNull();
    unmount();
    expect(gpu.disposed).toBe(1);
  });

  it('falls back to the older rig when there is no WebGL2', async () => {
    gpu.webgl2 = false;
    render(app('sprite:eulenfuchs'));
    expect(await screen.findByTestId('rigged')).toBeTruthy();
    expect(gpu.created).toBe(0);
  });

  it('falls back to the older rig when the relief cannot be loaded', async () => {
    vi.mocked(loadRelief).mockRejectedValue(new Error('chunk failed to load'));
    render(app('sprite:eulenfuchs'));
    expect(await screen.findByTestId('rigged')).toBeTruthy();
  });

  it('does not stay invisible when the relief load hangs', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    vi.mocked(loadRelief).mockImplementation(() => new Promise(() => {}));
    render(app('sprite:eulenfuchs'));
    // let the sprite set and the rig arrive (promises only), which starts the relief load
    for (let i = 0; i < 50 && screen.queryByTestId('procedural'); i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
    }
    expect(screen.queryByTestId('procedural')).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RELIEF_LOAD_TIMEOUT_MS - 500);
    });
    // still loading: nothing rather than a flash of the older rig
    expect(screen.queryByTestId('rigged')).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.queryByTestId('rigged')).not.toBeNull();
  });

  it('releases the relief renderer when switching to a creature that has none', async () => {
    const { rerender } = render(app('sprite:eulenfuchs'));
    await waitFor(() => expect(gpu.created).toBe(1));
    rerender(app('sprite:chamster'));
    expect(await screen.findByTestId('rigged')).toBeTruthy();
    expect(gpu.disposed).toBe(1);
  });
});
