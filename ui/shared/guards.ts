/**
 * Runtime type guards for IPC payloads, shared by the dashboard, the pet window and the parsers in
 * between. Every request answer arrives as `Record<string, unknown>`; these five helpers are the
 * only sanctioned way to narrow it.
 *
 * The house rule they exist to enforce: a value the core never sent stays `null`, and the screen
 * says "unknown". None of these functions invents a value — `str` only substitutes a fallback the
 * caller passed in deliberately, and `numOrNull`/`strOrNull` hand back `null` rather than `0`/`""`.
 */

export type Rec = Record<string, unknown>;

export function isRecord(x: unknown): x is Rec {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

/** A string, or the caller's explicit fallback (default: the empty string). */
export function str(x: unknown, fallback = ''): string {
  return typeof x === 'string' ? x : fallback;
}

/** A string, or `null` — for fields where "absent" and "empty" are different facts. */
export function strOrNull(x: unknown): string | null {
  return typeof x === 'string' ? x : null;
}

export function bool(x: unknown): boolean {
  return x === true;
}

/** A finite number, or `null`. `NaN` and `Infinity` count as absent. */
export function numOrNull(x: unknown): number | null {
  return typeof x === 'number' && Number.isFinite(x) ? x : null;
}

export function num(x: unknown, fallback: number): number {
  return numOrNull(x) ?? fallback;
}

/** The string members of an array; anything else yields an empty list. */
export function stringList(x: unknown): string[] {
  return Array.isArray(x) ? x.filter((v): v is string => typeof v === 'string') : [];
}
