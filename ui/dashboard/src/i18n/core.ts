/**
 * The translator itself, kept apart from `labels.ts` so the two can import each other's pieces
 * without a cycle: `core.ts` knows nothing about identifiers, `labels.ts` knows nothing about
 * language detection, and `index.ts` is the single import site for both.
 */

import { type Lang, pickLang } from '../../../shared/lang';
import type { Key } from './de';

export type { Key, Lang };
export { pickLang };
export type T = (key: Key) => string;

/**
 * The dictionaries loaded so far. Each language comes in two parts - what the shell and Heute say
 * (`loadLang`, awaited by `main.tsx` before the first render) and what the lazily loaded areas say
 * (`loadMore`) - so a reader downloads one language, and only the part the page needs (Spec
 * UX-Neubau §12). The test setup loads everything.
 */
const tables: Record<Lang, Partial<Record<Key, string>>> = { de: {}, en: {} };
const loaded = { de: false, en: false, deMore: false, enMore: false };
/** The page's language, as `loadLang` was last asked for it. */
let active: Lang = 'de';

/**
 * Every dictionary loaded so far. Complete once `loadLang` and `loadMore` have run for a language;
 * the test setup loads everything, so the parity tests see both full tables.
 */
export const DICT = tables as Record<Lang, Record<Key, string>>;

/** The start dictionary of `lang`. */
export async function loadLang(lang: Lang): Promise<void> {
  active = lang;
  if (lang === 'de' && !loaded.de) {
    Object.assign(tables.de, (await import('./de')).de);
    loaded.de = true;
  }
  if (lang === 'en' && !loaded.en) {
    Object.assign(tables.en, (await import('./en')).en);
    loaded.en = true;
  }
}

/** The copy of the lazily loaded areas, for `lang`; awaited before such an area renders. */
export async function loadMore(lang: Lang): Promise<void> {
  if (lang === 'de' && !loaded.deMore) {
    Object.assign(tables.de, (await import('./de.more')).deMore);
    loaded.deMore = true;
  }
  if (lang === 'en' && !loaded.enMore) {
    Object.assign(tables.en, (await import('./en.more')).enMore);
    loaded.enMore = true;
  }
}

/**
 * A lazily loaded module together with the area copy of the page's language - what every
 * `React.lazy` in the shell uses, so an area never renders before its words are there.
 */
export function withCopy<M>(load: () => Promise<M>): Promise<M> {
  return Promise.all([load(), loadMore(active)]).then(([module]) => module);
}

/** Whether `key` is in a loaded dictionary (`settingLabel` falls back to the path otherwise). */
export function hasKey(key: string): boolean {
  return key in tables[active] || key in tables.de;
}

/**
 * A translator for `lang`. A string not loaded in that language falls back to German, and one not
 * loaded at all to its key - never to nothing.
 */
export function translator(lang: Lang): T {
  const table = tables[lang];
  return (key: Key) => table[key] ?? tables.de[key] ?? key;
}

/** Substitute `{0}`, `{1}` … in a translated pattern. Missing arguments stay as written. */
export function fill(pattern: string, ...args: (string | number)[]): string {
  return pattern.replace(/\{(\d+)\}/g, (match, index: string) => {
    const value = args[Number(index)];
    return value === undefined ? match : String(value);
  });
}
