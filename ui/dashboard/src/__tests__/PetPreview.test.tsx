/**
 * The creature picker under `pet.variant`: names instead of ids, a picture instead of a guess.
 *
 * Pinned here: every value the core offers has a word in both languages (an option reading
 * `sprite:koalaflughund` is the failure this replaced), the picture points where the core serves
 * it, a drawn shape shows no picture rather than a broken one, and an id that could walk out of
 * the variants folder is never turned into a URL.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { optionLabel, translator } from '../i18n';
import { PetPreview, previewUrl } from '../pages/settings/PetPreview';

// The values `PetVariant` in src/nox/core/config/assistant.py offers. The Python side has its own
// test holding that list to the art on disk; this one holds it to the words.
const OFFERED = [
  'neutral',
  'imp',
  'fox',
  'owl',
  'cat',
  'sprite:meereswolf',
  'sprite:eulenfuchs',
  'sprite:katzendrache',
  'sprite:mottenkatze',
  'sprite:kaninchenkatze',
  'sprite:koalaflughund',
  'sprite:chamster',
];

describe('pet variant labels', () => {
  for (const lang of ['de', 'en'] as const) {
    it(`names every creature in ${lang} instead of showing its id`, () => {
      const t = translator(lang);
      for (const value of OFFERED) {
        const label = optionLabel(t, 'pet.variant', value);
        expect(label, value).not.toBe(value);
        expect(label).not.toContain('sprite:');
      }
    });
  }
});

describe('previewUrl', () => {
  it('points a photographed creature at the still frame the core serves', () => {
    expect(previewUrl('sprite:eulenfuchs')).toBe('/pet/variants/eulenfuchs/idle.png');
  });

  it('has no picture for a shape drawn in code', () => {
    expect(previewUrl('neutral')).toBeNull();
    expect(previewUrl('fox')).toBeNull();
  });

  it('never builds a URL out of an id that could leave the variants folder', () => {
    expect(previewUrl('sprite:../secrets')).toBeNull();
    expect(previewUrl('sprite:a/b')).toBeNull();
    expect(previewUrl('sprite:')).toBeNull();
  });
});

describe('PetPreview', () => {
  it('shows the creature, described by its name', () => {
    render(<PetPreview variant="sprite:chamster" alt="Chamster" />);
    const img = screen.getByRole('img', { name: 'Chamster' }) as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('/pet/variants/chamster/idle.png');
  });

  it('renders nothing for a drawn shape', () => {
    const { container } = render(<PetPreview variant="neutral" alt="Schlicht" />);
    expect(container.querySelector('img')).toBeNull();
  });

  it('hides itself when the picture cannot load, rather than showing a broken image', () => {
    const { container } = render(<PetPreview variant="sprite:meereswolf" alt="Meereswolf" />);
    fireEvent.error(container.querySelector('img') as HTMLImageElement);
    expect(container.querySelector('img')).toBeNull();
  });
});
