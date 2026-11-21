/**
 * How fast the pet may draw: `pet.fps_target` normally, `pet.fps_in_game` while a game runs or
 * something owns the screen. The shell knows which (it looks at the machine; the page cannot) and
 * tells the page by setting `window.__noxPetFrames` and firing a `nox-pet-frames` event. Pure
 * functions plus one hook; the renderers only see the resulting `fps` in the animation parameters.
 */
import { useEffect, useState } from 'react';

export interface FrameBudget {
  /** Frame rate in normal use (`pet.fps_target`). */
  target: number;
  /** Frame rate while a game is active (`pet.fps_in_game`). */
  inGame: number;
  /** Whether the shell says a game runs or the screen is taken over. */
  gameActive: boolean;
}

export const DEFAULT_BUDGET: FrameBudget = { target: 60, inGame: 20, gameActive: false };
export const FRAMES_EVENT = 'nox-pet-frames';
export const FRAMES_GLOBAL = '__noxPetFrames';
const FPS_MIN = 1;
const FPS_MAX = 240;

function boundedFps(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isInteger(value) && value >= FPS_MIN && value <= FPS_MAX
    ? value
    : fallback;
}

/** What the shell sent, made safe: anything missing or out of range keeps its default. */
export function parseBudget(raw: unknown): FrameBudget {
  if (typeof raw !== 'object' || raw === null) return DEFAULT_BUDGET;
  const message = raw as Record<string, unknown>;
  return {
    target: boundedFps(message.fpsTarget, DEFAULT_BUDGET.target),
    inGame: boundedFps(message.fpsInGame, DEFAULT_BUDGET.inGame),
    gameActive: message.gameActive === true,
  };
}

/** The most the pet may draw per second right now. The in-game rate never exceeds the target. */
export function fpsCeiling(budget: FrameBudget): number {
  return budget.gameActive ? Math.min(budget.target, budget.inGame) : budget.target;
}

function readBudget(): FrameBudget {
  return parseBudget((window as unknown as Record<string, unknown>)[FRAMES_GLOBAL]);
}

/** The current budget; follows the shell's `nox-pet-frames` events. */
export function useFrameBudget(): FrameBudget {
  const [budget, setBudget] = useState<FrameBudget>(readBudget);
  useEffect(() => {
    const update = () => setBudget(readBudget());
    update(); // the shell may have spoken between the first render and this effect
    window.addEventListener(FRAMES_EVENT, update);
    return () => window.removeEventListener(FRAMES_EVENT, update);
  }, []);
  return budget;
}
