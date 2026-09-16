import { describe, expect, it } from 'vitest';

import {
  CONCEPT_VARIANT_IDS,
  DEFAULT_VARIANT_ID,
  VARIANTS,
  VARIANT_IDS,
  getVariant,
  type PetVariant,
} from '../variants';

const REQUIRED_KEYS: (keyof PetVariant)[] = [
  'id', 'name', 'description', 'bodyWidth', 'bodyHeight', 'earShape', 'earSize', 'tailShape',
  'tailLength', 'eyeStyle', 'eyeSize', 'outlineWeight', 'idleMotionAmplitude', 'palette',
];

/** WCAG relative luminance / contrast ratio, used only as a sanity bound here (this is an
 * illustration, not text), converting the stored HSL palette to sRGB first. */
function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const hp = ((h % 360) + 360) % 360 / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  let [r, g, b] = [0, 0, 0];
  if (hp < 1) [r, g, b] = [c, x, 0];
  else if (hp < 2) [r, g, b] = [x, c, 0];
  else if (hp < 3) [r, g, b] = [0, c, x];
  else if (hp < 4) [r, g, b] = [0, x, c];
  else if (hp < 5) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  const m = l - c / 2;
  return [r + m, g + m, b + m];
}

function relLuminance([r, g, b]: [number, number, number]): number {
  const f = (v: number) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function contrastRatio(l1: number, l2: number): number {
  const [hi, lo] = l1 >= l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

const LIGHT_BG = relLuminance([0.96, 0.96, 0.97]); // near-white host background
const DARK_BG = relLuminance([0.04, 0.04, 0.07]); // near-black host background (#0b0b12-ish)

describe('variant registry', () => {
  it('registers neutral plus exactly the four OP-1 concept variants', () => {
    expect(DEFAULT_VARIANT_ID).toBe('neutral');
    expect([...VARIANT_IDS].sort()).toEqual(['cat', 'fox', 'imp', 'neutral', 'owl'].sort());
    expect([...CONCEPT_VARIANT_IDS].sort()).toEqual(['cat', 'fox', 'imp', 'owl'].sort());
    expect(CONCEPT_VARIANT_IDS).not.toContain('neutral');
  });

  it('every variant has all required fields with sane ranges', () => {
    for (const [id, variant] of Object.entries(VARIANTS)) {
      for (const key of REQUIRED_KEYS) {
        expect(variant[key], `${id}.${String(key)}`).toBeDefined();
      }
      expect(variant.id).toBe(id);
      expect(variant.name.length).toBeGreaterThan(0);
      expect(variant.description.length).toBeGreaterThan(0);
      expect(variant.bodyWidth).toBeGreaterThan(0);
      expect(variant.bodyHeight).toBeGreaterThan(0);
      expect(variant.earSize).toBeGreaterThanOrEqual(0);
      expect(variant.earSize).toBeLessThanOrEqual(1.5);
      expect(variant.tailLength).toBeGreaterThanOrEqual(0);
      expect(variant.tailLength).toBeLessThanOrEqual(1.5);
      expect(variant.eyeSize).toBeGreaterThan(0);
      expect(variant.outlineWeight).toBeGreaterThanOrEqual(0);
      expect(variant.idleMotionAmplitude).toBeGreaterThan(0);
      if (variant.earShape === 'none') expect(variant.earSize).toBe(0);
      if (variant.tailShape === 'none') expect(variant.tailLength).toBe(0);

      const pal = variant.palette;
      expect(pal.bodyHue).toBeGreaterThanOrEqual(0);
      expect(pal.bodyHue).toBeLessThan(360);
      expect(pal.accentHue).toBeGreaterThanOrEqual(0);
      expect(pal.accentHue).toBeLessThan(360);
      for (const v of [pal.bodySat, pal.bodyLightOnLight, pal.bodyLightOnDark]) {
        expect(v).toBeGreaterThanOrEqual(0);
        expect(v).toBeLessThanOrEqual(1);
      }
    }
  });

  it('every variant is visually distinguishable from every other by silhouette or palette', () => {
    const signatures = new Set<string>();
    for (const variant of Object.values(VARIANTS)) {
      const sig = [
        variant.earShape,
        variant.tailShape,
        variant.eyeStyle,
        Math.round(variant.bodyWidth * 10),
        Math.round(variant.bodyHeight * 10),
        Math.round(variant.palette.bodyHue / 15),
      ].join('|');
      expect(signatures.has(sig), `duplicate silhouette signature: ${sig}`).toBe(false);
      signatures.add(sig);
    }
  });

  it('each palette has usable contrast against both a light and a dark host background', () => {
    for (const [id, variant] of Object.entries(VARIANTS)) {
      const { bodyHue, bodySat, bodyLightOnLight, bodyLightOnDark } = variant.palette;
      const lumOnLight = relLuminance(hslToRgb(bodyHue, bodySat, bodyLightOnLight));
      const lumOnDark = relLuminance(hslToRgb(bodyHue, bodySat, bodyLightOnDark));
      expect(contrastRatio(lumOnLight, LIGHT_BG), `${id} vs light bg`).toBeGreaterThan(1.5);
      expect(contrastRatio(lumOnDark, DARK_BG), `${id} vs dark bg`).toBeGreaterThan(1.5);
    }
  });

  it('getVariant falls back to neutral for unknown or missing ids', () => {
    expect(getVariant(null).id).toBe('neutral');
    expect(getVariant(undefined).id).toBe('neutral');
    expect(getVariant('')).toBe(VARIANTS.neutral);
    expect(getVariant('does-not-exist').id).toBe('neutral');
    expect(getVariant('imp').id).toBe('imp');
  });
});
