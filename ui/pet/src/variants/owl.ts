/**
 * owl: a round owl-like nightling — calm, watchful, unhurried. A plump rounded body with small
 * feather-tuft ears, no visible tail (owls read as round when perched) and large wide-oval eyes
 * that do most of the expressive work. Deep indigo body with a pale moon-glow accent. Idle motion
 * is deliberately the slowest of the four variants (a calm night watcher, not an energetic mascot,
 * A73), while still carrying all 21 expressions through eye/brow/mouth posture (D34).
 */

import type { PetVariant } from './types';

export const owl: PetVariant = {
  id: 'owl',
  name: 'Nightling-Owl',
  description: 'Rundlicher Nacht-Uhu — ruhig, wachsam, mit großen Augen und kaum sichtbarem Schwanz.',
  bodyWidth: 1.15,
  bodyHeight: 1.1,
  earShape: 'owl-tufts',
  earSize: 0.5,
  tailShape: 'none',
  tailLength: 0,
  eyeStyle: 'wide-oval',
  eyeSize: 1.2,
  outlineWeight: 2.5,
  idleMotionAmplitude: 0.7,
  palette: {
    bodyHue: 248,
    bodySat: 0.5,
    bodyLightOnLight: 0.36,
    bodyLightOnDark: 0.58,
    accentHue: 205,
  },
};
