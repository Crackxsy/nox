/**
 * cat: a sleek cat-like shadow — quiet, precise, faintly glowing. A slender body, close-set
 * triangular ears, a slim curling tail and narrow slit eyes with a thin sharp outline give it a
 * "shadow creature" read distinct from the impish variant despite sharing the slit eye style
 * (differentiated by silhouette, palette and motion, not by eye shape alone). Deep plum body with a faint
 * magenta-glow accent; the second-lowest idle amplitude of the four
 * (composed, deliberate movement) with all 21 expressions carried through posture (D34).
 */

import type { PetVariant } from './types';

export const cat: PetVariant = {
  id: 'cat',
  name: 'Shadow-Cat',
  description: 'Schlanker Schatten-Kater — leise, präzise, mit schmalen Augen und feinem Umriss.',
  bodyWidth: 0.9,
  bodyHeight: 0.92,
  earShape: 'cat-ears',
  earSize: 0.75,
  tailShape: 'sleek-curl',
  tailLength: 0.85,
  eyeStyle: 'slit',
  eyeSize: 0.95,
  outlineWeight: 1,
  idleMotionAmplitude: 0.95,
  palette: {
    bodyHue: 291,
    bodySat: 0.45,
    bodyLightOnLight: 0.3,
    bodyLightOnDark: 0.5,
    accentHue: 318,
  },
};
