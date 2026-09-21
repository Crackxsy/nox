/**
 * Theme plumbing for the pet window (#25).
 *
 * Two jobs, both small:
 *  - `useThemeMode()` tells React which of the shared token themes is live, so the renderer can
 *    pick the variant palette lightness that was tuned for that background (`bodyLightOnLight` vs
 *    `bodyLightOnDark`, see variants/types.ts). The pet window has no theme switcher; it follows
 *    `prefers-color-scheme`, and honours an explicit `data-theme` on <html> if something sets one
 *    (same guard order as `ui/shared/tokens.css`).
 *  - `readChrome()` pulls the few token values the *canvas* needs (it cannot use `var(...)`) out of
 *    the live computed style, so the status label under the creature is drawn in the same ink and
 *    material as the DOM chips instead of a second hard-coded palette.
 */

import { useEffect, useState } from 'react';

export type ThemeMode = 'light' | 'dark';

const DARK_QUERY = '(prefers-color-scheme: dark)';

export function currentThemeMode(): ThemeMode {
  if (typeof document === 'undefined') return 'dark';
  const explicit = document.documentElement.getAttribute('data-theme');
  if (explicit === 'dark') return 'dark';
  if (explicit === 'light') return 'light';
  return typeof window !== 'undefined' && window.matchMedia?.(DARK_QUERY).matches ? 'dark' : 'light';
}

export function useThemeMode(): ThemeMode {
  const [mode, setMode] = useState<ThemeMode>(currentThemeMode);
  useEffect(() => {
    const update = () => setMode(currentThemeMode());
    const mq = window.matchMedia?.(DARK_QUERY);
    mq?.addEventListener('change', update);
    // An explicit data-theme wins over the media query, so watch the attribute too.
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    update();
    return () => {
      mq?.removeEventListener('change', update);
      observer.disconnect();
    };
  }, []);
  return mode;
}

/** Token values the canvas renderer needs as literal colour strings. */
export interface PetChrome {
  /** `--pet-ink`: the creature's line work (eyes, brows, mouth, outline). */
  ink: string;
  /** `--pet-chip-bg`: translucent material behind the status label. */
  chipBg: string;
  /** `--pet-chip-ink`: text on that material. */
  chipInk: string;
  /** `--pet-chip-line`: hairline around it. */
  chipLine: string;
  /** `--ok`: the privacy shield ring. */
  ok: string;
  /** `--danger`: the error ring and the muted cross. */
  danger: string;
  /** `--ink-2`: the dashed "no information" ring drawn when Nox is offline or unavailable. */
  muted: string;
}

/** Fallbacks match `ui/pet/src/styles.css` / `ui/shared/tokens.css` so a headless render (or a
 * still frame taken before the stylesheet applies) still draws something legible. */
export const CHROME_FALLBACK: Record<ThemeMode, PetChrome> = {
  light: {
    ink: '#1d1d1f',
    chipBg: 'rgba(255,255,255,0.72)',
    chipInk: '#1d1d1f',
    chipLine: '#d2d2d7',
    ok: '#17753a',
    danger: '#bb2a1e',
    muted: '#6e6e73',
  },
  dark: {
    ink: '#1d1d1f',
    chipBg: 'rgba(0,0,0,0.72)',
    chipInk: '#f5f5f7',
    chipLine: '#424245',
    ok: '#41c977',
    danger: '#ff8a80',
    muted: '#86868b',
  },
};

export function readChrome(mode: ThemeMode): PetChrome {
  const fallback = CHROME_FALLBACK[mode];
  if (typeof document === 'undefined' || typeof getComputedStyle !== 'function') return fallback;
  const style = getComputedStyle(document.documentElement);
  const pick = (name: string, fb: string): string => style.getPropertyValue(name).trim() || fb;
  return {
    ink: pick('--pet-ink', fallback.ink),
    chipBg: pick('--pet-chip-bg', fallback.chipBg),
    chipInk: pick('--pet-chip-ink', fallback.chipInk),
    chipLine: pick('--pet-chip-line', fallback.chipLine),
    ok: pick('--ok', fallback.ok),
    danger: pick('--danger', fallback.danger),
    muted: pick('--ink-2', fallback.muted),
  };
}
