/**
 * The translator itself, kept apart from `labels.ts` so the two can import each other's pieces
 * without a cycle: `core.ts` knows nothing about identifiers, `labels.ts` knows nothing about
 * language detection, and `index.ts` is the single import site for both.
 */

import { type Lang, pickLang } from '../../../shared/lang';
import { type Key, de } from './de';
import { en } from './en';

export type { Key, Lang };
export { pickLang };
export type T = (key: Key) => string;

export const DICT: Record<Lang, Record<Key, string>> = { de, en };

export function translator(lang: Lang): T {
  const table = DICT[lang];
  return (key: Key) => table[key];
}

/** Substitute `{0}`, `{1}` … in a translated pattern. Missing arguments stay as written. */
export function fill(pattern: string, ...args: (string | number)[]): string {
  return pattern.replace(/\{(\d+)\}/g, (match, index: string) => {
    const value = args[Number(index)];
    return value === undefined ? match : String(value);
  });
}
