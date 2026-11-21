import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import {
  DEFAULT_BUDGET,
  FRAMES_EVENT,
  FRAMES_GLOBAL,
  fpsCeiling,
  parseBudget,
  useFrameBudget,
} from '../frameBudget';
import { fpsFor } from '../petState';

const tell = (message: unknown) => {
  (window as unknown as Record<string, unknown>)[FRAMES_GLOBAL] = message;
  window.dispatchEvent(new CustomEvent(FRAMES_EVENT));
};

afterEach(() => {
  delete (window as unknown as Record<string, unknown>)[FRAMES_GLOBAL];
});

describe('parseBudget', () => {
  it('reads what the shell sends', () => {
    expect(parseBudget({ fpsTarget: 90, fpsInGame: 15, gameActive: true })).toEqual({
      target: 90,
      inGame: 15,
      gameActive: true,
    });
  });

  it.each([undefined, null, 'fast', 7, []])('keeps the defaults for %j', (raw) => {
    expect(parseBudget(raw)).toEqual(DEFAULT_BUDGET);
  });

  it.each([0, -5, 241, 30.5, '30', NaN, Infinity])('rejects a frame rate of %j', (bad) => {
    const budget = parseBudget({ fpsTarget: bad, fpsInGame: bad, gameActive: false });
    expect(budget.target).toBe(DEFAULT_BUDGET.target);
    expect(budget.inGame).toBe(DEFAULT_BUDGET.inGame);
  });

  it('counts a game as active only for a real true', () => {
    expect(parseBudget({ gameActive: 'yes' }).gameActive).toBe(false);
    expect(parseBudget({ gameActive: 1 }).gameActive).toBe(false);
  });
});

describe('fpsCeiling', () => {
  it('is the target in normal use and the in-game rate while a game is active', () => {
    expect(fpsCeiling({ target: 60, inGame: 20, gameActive: false })).toBe(60);
    expect(fpsCeiling({ target: 60, inGame: 20, gameActive: true })).toBe(20);
  });

  it('never lets the in-game rate exceed the target', () => {
    expect(fpsCeiling({ target: 15, inGame: 30, gameActive: true })).toBe(15);
  });
});

describe('fpsFor with a ceiling', () => {
  it('keeps the old rates under the default ceiling', () => {
    expect(fpsFor('none', 'idle', true)).toBe(60);
    expect(fpsFor('normal', 'idle', true)).toBe(30);
  });

  it('lets a higher target raise the top tier only', () => {
    expect(fpsFor('none', 'idle', true, 144)).toBe(144);
    expect(fpsFor('normal', 'idle', true, 144)).toBe(30);
  });

  it('caps every tier at the in-game rate', () => {
    expect(fpsFor('none', 'idle', true, 20)).toBe(20);
    expect(fpsFor('normal', 'idle', true, 20)).toBe(20);
    expect(fpsFor('high_load', 'idle', true, 20)).toBe(15);
    expect(fpsFor('none', 'unavailable', true, 5)).toBe(5);
    expect(fpsFor('none', 'idle', false, 5)).toBe(5);
  });
});

describe('useFrameBudget', () => {
  it('starts from the defaults and follows the shell', () => {
    const { result } = renderHook(() => useFrameBudget());
    expect(result.current).toEqual(DEFAULT_BUDGET);
    act(() => tell({ fpsTarget: 60, fpsInGame: 12, gameActive: true }));
    expect(fpsCeiling(result.current)).toBe(12);
    act(() => tell({ fpsTarget: 60, fpsInGame: 12, gameActive: false }));
    expect(fpsCeiling(result.current)).toBe(60);
  });

  it('picks up what the shell said before the page was ready', () => {
    (window as unknown as Record<string, unknown>)[FRAMES_GLOBAL] = {
      fpsTarget: 60,
      fpsInGame: 25,
      gameActive: true,
    };
    const { result } = renderHook(() => useFrameBudget());
    expect(fpsCeiling(result.current)).toBe(25);
  });

  it('stops listening when the page goes away', () => {
    const { result, unmount } = renderHook(() => useFrameBudget());
    unmount();
    tell({ fpsTarget: 60, fpsInGame: 5, gameActive: true });
    expect(result.current).toEqual(DEFAULT_BUDGET);
  });
});
