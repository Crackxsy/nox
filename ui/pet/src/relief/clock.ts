/**
 * The relief pet's frame clock: animation time and the step since the last frame.
 *
 * Kept apart from the component so its two promises can be tested: a step is never negative
 * (a `visibilitychange` or a timer that reports an earlier time than the last frame must not run
 * the springs backwards) and never longer than `MAX_STEP_S` (a stalled tab must not replay its
 * stall), and time spent hidden is cut out rather than replayed.
 */

export const MAX_STEP_S = 0.1;

export class FrameClock {
  private startMs: number;
  private lastMs: number;

  constructor(nowMs: number) {
    this.startMs = nowMs;
    this.lastMs = nowMs;
  }

  /** Seconds of animation time at `nowMs`, and the step since the previous tick (0..MAX_STEP_S). */
  tick(nowMs: number, first = false): { t: number; dt: number } {
    const raw = (nowMs - this.lastMs) / 1000;
    const dt = first || !Number.isFinite(raw) ? 0 : Math.min(MAX_STEP_S, Math.max(0, raw));
    this.lastMs = Math.max(this.lastMs, nowMs);
    return { t: (this.lastMs - this.startMs) / 1000, dt };
  }

  /** The window is visible again: drop the time it was hidden from the animation. */
  resume(nowMs: number): void {
    if (nowMs > this.lastMs) {
      this.startMs += nowMs - this.lastMs;
      this.lastMs = nowMs;
    }
  }
}

/**
 * Follow a media query (`prefers-reduced-motion`, the device pixel ratio) while the pet runs: the
 * setting can change under a running window, and a pet that read it once would keep moving after
 * the user asked it to stop. Returns the unsubscribe function.
 */
export function watchMedia(query: string, onChange: (matches: boolean) => void): () => void {
  const mq = typeof window !== 'undefined' ? window.matchMedia?.(query) : undefined;
  if (!mq) return () => {};
  const listener = (e: MediaQueryListEvent): void => onChange(e.matches);
  mq.addEventListener('change', listener);
  return () => mq.removeEventListener('change', listener);
}

/**
 * Call `onChange` whenever the device pixel ratio changes (window dragged to another monitor,
 * zoom). A resolution query only matches one ratio, so it is re-armed after every change.
 */
export function watchPixelRatio(onChange: (ratio: number) => void): () => void {
  let stop = (): void => {};
  const arm = (): void => {
    const ratio = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
    stop = watchMedia(`(resolution: ${ratio}dppx)`, () => {
      stop();
      const next = window.devicePixelRatio || 1;
      onChange(next);
      arm();
    });
  };
  arm();
  return () => stop();
}
