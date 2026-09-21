/**
 * The design tokens, measured rather than asserted by eye.
 *
 * Two things this pins. First, the two dark-mode blocks in `tokens.css` are declared twice on
 * purpose (one for `prefers-color-scheme`, one for an explicit `data-theme`) and must never drift
 * apart — a palette that is right in one of them and stale in the other is worse than no palette.
 *
 * Second, the contrast ratios in the file's own docstring are real. The previous palette claimed a
 * designed dark mode and measured 1.25:1 for its tile surfaces and 3.02:1 for white on every
 * primary button; numbers in a comment are not a design system, numbers in a test are.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * The very file both apps ship, read from disk rather than mirrored here — a copy of the values
 * would pass while the palette rotted. vitest runs from `ui/dashboard` or `ui/pet`; the shared
 * folder is one level up from either.
 */
const CSS = readFileSync(join(process.cwd(), '..', 'shared', 'tokens.css'), 'utf8');

/** All `--name: value;` declarations inside the first block matching `selector`. */
function block(selector: string): Record<string, string> {
  const start = CSS.indexOf(selector);
  expect(start, `selector not found: ${selector}`).toBeGreaterThan(-1);
  const open = CSS.indexOf('{', start);
  let depth = 0;
  let end = open;
  for (let i = open; i < CSS.length; i++) {
    if (CSS[i] === '{') depth++;
    if (CSS[i] === '}') {
      depth--;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  const body = CSS.slice(open + 1, end);
  const out: Record<string, string> = {};
  for (const [, name, value] of body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    out[name] = value.trim();
  }
  return out;
}

const light = block(':root {');
const darkAuto = block(':root:not([data-theme="light"]) {');
const darkExplicit = block(':root[data-theme="dark"] {');

function srgb(channel: number): number {
  const c = channel / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const value = hex.trim().replace('#', '');
  expect(value, `not a plain hex colour: ${hex}`).toMatch(/^[0-9a-fA-F]{6}$/);
  const r = Number.parseInt(value.slice(0, 2), 16);
  const g = Number.parseInt(value.slice(2, 4), 16);
  const b = Number.parseInt(value.slice(4, 6), 16);
  return 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
}

/** WCAG 2.1 contrast ratio between two opaque colours. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function resolve(palette: Record<string, string>, name: string): string {
  const value = palette[name];
  expect(value, `token missing: ${name}`).toBeDefined();
  return value;
}

describe('the dark palette is declared twice and identical both times', () => {
  it('the media-query block and the explicit block agree on every token', () => {
    expect(Object.keys(darkAuto).sort()).toEqual(Object.keys(darkExplicit).sort());
    for (const key of Object.keys(darkAuto)) {
      expect(darkExplicit[key], key).toBe(darkAuto[key]);
    }
  });

  it('dark overrides every colour the light palette defines', () => {
    const colourish = Object.keys(light).filter((k) => /^#|^rgba/.test(light[k]));
    for (const key of colourish) {
      // `--danger-action*` is global on purpose: one destructive colour in both themes.
      if (key.startsWith('--danger-action')) continue;
      expect(darkAuto[key], `dark mode does not redefine ${key}`).toBeDefined();
    }
  });
});

describe('measured contrast', () => {
  const cases: { name: string; fg: string; bg: string; min: number }[] = [
    { name: 'ink on page', fg: '--ink', bg: '--page', min: 4.5 },
    { name: 'ink-2 on page', fg: '--ink-2', bg: '--page', min: 4.5 },
    { name: 'ink-2 on tile', fg: '--ink-2', bg: '--tile', min: 4.5 },
    { name: 'link on page', fg: '--link', bg: '--page', min: 4.5 },
    { name: 'link on tile', fg: '--link', bg: '--tile', min: 4.5 },
    { name: 'blue-ink on blue (every primary button)', fg: '--blue-ink', bg: '--blue', min: 4.5 },
    { name: 'eyebrow on tile', fg: '--eyebrow', bg: '--tile', min: 4.5 },
    { name: 'ok on tile', fg: '--ok', bg: '--tile', min: 4.5 },
    { name: 'warn on tile', fg: '--warn', bg: '--tile', min: 4.5 },
    { name: 'danger on tile', fg: '--danger', bg: '--tile', min: 4.5 },
    { name: 'feature ink on feature tile', fg: '--tile-feature-ink', bg: '--tile-feature', min: 4.5 },
    {
      name: 'feature ink-2 on feature tile',
      fg: '--tile-feature-ink-2',
      bg: '--tile-feature',
      min: 4.5,
    },
    { name: 'hairline on page', fg: '--hairline', bg: '--page', min: 3 },
    { name: 'feature tile vs page', fg: '--tile-feature', bg: '--page', min: 3 },
    { name: 'switch off track vs tile', fg: '--switch-off', bg: '--tile', min: 3 },
    { name: 'switch knob vs off track', fg: '--switch-knob', bg: '--switch-off', min: 3 },
    { name: 'switch knob vs on track', fg: '--switch-knob', bg: '--switch-on', min: 3 },
  ];

  for (const theme of ['light', 'dark'] as const) {
    const palette = theme === 'light' ? light : { ...light, ...darkAuto };
    for (const c of cases) {
      it(`${theme}: ${c.name} >= ${c.min}:1`, () => {
        const ratio = contrast(resolve(palette, c.fg), resolve(palette, c.bg));
        expect(ratio, `${theme} ${c.name} measured ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
          c.min,
        );
      });
    }
  }

  it('the one destructive pair carries its text in both themes and against both pages', () => {
    const action = resolve(light, '--danger-action');
    const ink = resolve(light, '--danger-action-ink');
    expect(contrast(ink, action)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(action, resolve(light, '--page'))).toBeGreaterThanOrEqual(3);
    expect(contrast(action, resolve({ ...light, ...darkAuto }, '--page'))).toBeGreaterThanOrEqual(3);
    // The armed shade is darker still, so arming reads as an escalation, not as a different button.
    expect(luminance(resolve(light, '--danger-action-armed'))).toBeLessThan(luminance(action));
  });
});

describe('the scales exist at all', () => {
  it('defines a spacing scale, a type scale and one motion curve', () => {
    for (const n of [1, 2, 3, 4, 5, 6, 7, 8, 9]) expect(light[`--s-${n}`]).toBeDefined();
    for (const n of ['2xs', 'xs', 'sm', 'base', 'md', 'lg']) {
      expect(light[`--text-${n}`]).toBeDefined();
    }
    expect(light['--ease']).toBeDefined();
    expect(light['--dur-fast']).toBeDefined();
    expect(light['--dur-base']).toBeDefined();
    expect(light['--dur-slow']).toBeDefined();
  });

  it('every spacing step is a multiple of four', () => {
    for (const n of [1, 2, 3, 4, 5, 6, 7, 8, 9]) {
      const px = Number.parseInt(light[`--s-${n}`], 10);
      expect(px % 4, `--s-${n} is ${px}px`).toBe(0);
    }
  });
});
