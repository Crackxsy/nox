/**
 * Variant registry: looked up from the `?variant=` query flag in main.tsx. Unknown or
 * missing ids fall back to `neutral` (the default until a final design is chosen).
 */

import { cat } from './cat';
import { fox } from './fox';
import { imp } from './imp';
import { neutral } from './neutral';
import { owl } from './owl';
import type { PetVariant } from './types';

export * from './types';
export { cat, fox, imp, neutral, owl };

export const DEFAULT_VARIANT_ID = 'neutral';

/** neutral (placeholder default) plus the four concept variants. */
export const VARIANTS: Readonly<Record<string, PetVariant>> = {
  neutral,
  imp,
  fox,
  owl,
  cat,
};

export const VARIANT_IDS: readonly string[] = Object.keys(VARIANTS);

/** The four gallery candidates, excluding the neutral placeholder. */
export const CONCEPT_VARIANT_IDS: readonly string[] = ['imp', 'fox', 'owl', 'cat'];

export function getVariant(id: string | null | undefined): PetVariant {
  if (id && Object.prototype.hasOwnProperty.call(VARIANTS, id)) return VARIANTS[id];
  return VARIANTS[DEFAULT_VARIANT_ID];
}
