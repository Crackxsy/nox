/**
 * The dashboard's design tokens (Spec UX-Neubau §11), measured rather than asserted by eye: the two
 * dark blocks agree, and every pair the spec's contrast table names reaches its WCAG minimum - text
 * 4.5:1, the borders of inputs and switches (`--ctl`) 3:1 (WCAG 1.4.11).
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const CSS = readFileSync(join(process.cwd(), 'src', 'tokens.css'), 'utf8');

function block(selector: string): Record<string, string> {
  const start = CSS.indexOf(selector);
  expect(start, `selector not found: ${selector}`).toBeGreaterThan(-1);
  const open = CSS.indexOf('{', start);
  let depth = 0;
  let end = open;
  for (let i = open; i < CSS.length; i++) {
    if (CSS[i] === '{') depth++;
    if (CSS[i] === '}' && --depth === 0) {
      end = i;
      break;
    }
  }
  const out: Record<string, string> = {};
  for (const [, name, value] of CSS.slice(open + 1, end).matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    out[name!] = value!.trim();
  }
  return out;
}

const light = block(':root {');
const darkAuto = block(':root:not([data-theme="light"]) {');
const darkExplicit = block(':root[data-theme="dark"] {');

function channel(c: number): number {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const v = hex.replace('#', '');
  expect(v, `not a plain hex colour: ${hex}`).toMatch(/^[0-9a-fA-F]{6}$/);
  const [r, g, b] = [0, 2, 4].map((i) => Number.parseInt(v.slice(i, i + 2), 16));
  return 0.2126 * channel(r!) + 0.7152 * channel(g!) + 0.0722 * channel(b!);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

describe('the dark palette', () => {
  it('is declared twice and identical both times', () => {
    expect(darkExplicit).toEqual(darkAuto);
  });

  it('redefines every colour of the light palette', () => {
    for (const [key, value] of Object.entries(light)) {
      if (!value.startsWith('#')) continue;
      expect(darkAuto[key], `dark mode does not redefine ${key}`).toBeDefined();
    }
  });
});

describe('contrast (Spec §10 table)', () => {
  const cases: [string, string, string, number][] = [
    ['text on ground', '--ink', '--bg', 4.5],
    ['text 2 on ground', '--ink-2', '--bg', 4.5],
    ['quiet text on ground', '--ink-3', '--bg', 4.5],
    ['quiet text on the surface below a field', '--ink-3', '--bg-2', 4.5],
    ['quiet text on the waiting box', '--ink-3', '--accent-wash', 4.5],
    ['accent text on the waiting box', '--accent-ink', '--accent-wash', 4.5],
    ['red on ground', '--danger', '--bg', 4.5],
    ['red on the stop strip', '--danger', '--danger-wash', 4.5],
    ['green on its wash', '--ok', '--ok-wash', 4.5],
    ['control border on ground (1.4.11)', '--ctl', '--bg', 3],
    ['control border on the surface (1.4.11)', '--ctl', '--surface', 3],
  ];
  for (const theme of ['light', 'dark'] as const) {
    const palette = theme === 'light' ? light : { ...light, ...darkAuto };
    for (const [name, fg, bg, min] of cases) {
      it(`${theme}: ${name} >= ${min}:1`, () => {
        const ratio = contrast(palette[fg]!, palette[bg]!);
        expect(ratio, `${theme} ${name}: ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(min);
      });
    }
    it(`${theme}: white on the Stopp fill and ink on the lamp`, () => {
      expect(contrast('#ffffff', palette['--danger-fill']!)).toBeGreaterThanOrEqual(4.5);
      expect(contrast('#15171a', palette['--lamp']!)).toBeGreaterThanOrEqual(4.5);
    });
  }
});
