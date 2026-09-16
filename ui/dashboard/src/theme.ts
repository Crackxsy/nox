/**
 * Appearance preference (System / Light / Dark).
 *
 * "System" is the default and simply leaves `<html>` without a `data-theme` attribute, so the
 * stylesheet's `prefers-color-scheme` block decides. An explicit choice sets
 * `data-theme="light"|"dark"` and is remembered in `localStorage` under `nox.theme`.
 * Storage access is wrapped: a locked-down browser profile must degrade to "System", never throw.
 */

export type ThemePref = 'system' | 'light' | 'dark';

export const THEME_KEY = 'nox.theme';
export const THEME_PREFS: readonly ThemePref[] = ['system', 'light', 'dark'];

function isPref(value: unknown): value is ThemePref {
  return value === 'system' || value === 'light' || value === 'dark';
}

/** The stored preference, or "system" when nothing valid is stored. */
export function readTheme(): ThemePref {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    return isPref(stored) ? stored : 'system';
  } catch {
    return 'system';
  }
}

export function storeTheme(pref: ThemePref): void {
  try {
    window.localStorage.setItem(THEME_KEY, pref);
  } catch {
    // Private mode / blocked storage: the choice still applies for this session.
  }
}

/** Reflects the preference on `<html>`; "system" removes the attribute again. */
export function applyTheme(pref: ThemePref): void {
  const root = document.documentElement;
  if (pref === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', pref);
}
