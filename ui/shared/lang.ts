/**
 * Language choice, shared by both front-ends.
 *
 * The dashboard has a 400-key dictionary and the pet window has eight strings, but they must agree
 * on *which* language is active — a German dashboard next to a bilingual pet chip was two people's
 * conventions in one product. This module owns the decision; each app owns its own words.
 */

export type Lang = 'de' | 'en';

/** `?lang=de|en` wins, then the browser's preference, then German (FR-5.3: DE first). */
export function pickLang(search: string, navigatorLanguages: readonly string[] = []): Lang {
  const q = new URLSearchParams(search).get('lang');
  if (q === 'de' || q === 'en') return q;
  for (const l of navigatorLanguages) {
    if (l.toLowerCase().startsWith('de')) return 'de';
    if (l.toLowerCase().startsWith('en')) return 'en';
  }
  return 'de';
}
