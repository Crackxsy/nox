/**
 * Whether the first start was finished or skipped in this browser. Kept apart from the flow itself
 * so the shell can ask without loading it.
 */

export const FIRST_RUN_KEY = 'nox.firstRun';

export function firstRunDone(): boolean {
  try {
    return window.localStorage.getItem(FIRST_RUN_KEY) === 'done';
  } catch {
    return false;
  }
}

export function markFirstRunDone(): void {
  try {
    window.localStorage.setItem(FIRST_RUN_KEY, 'done');
  } catch {
    // Blocked storage: the flow simply finishes for this session.
  }
}
