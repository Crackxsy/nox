/**
 * The dashboard's language layer, in four files: `de.ts` and `en.ts` hold the copy, `core.ts` the
 * translator and language choice, `labels.ts` the identifier → word tables. This file is what the
 * rest of the app imports.
 *
 * `en.ts` is typed `Record<Key, string>` against the German dictionary, so a missing key is a
 * compile error rather than a runtime fallback — the old "fall back to German on screen" branch
 * could not be reached any more and is gone.
 */

export * from './core';
export * from './labels';
