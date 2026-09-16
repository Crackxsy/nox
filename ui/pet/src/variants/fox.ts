/**
 * fox: a fox-eared spirit of the twilight hour — alert, warm, a little sly. Tall pointed ears with
 * pale amber inner markings, a fluffy amber-tipped tail and softly narrowed "grinning" crescent
 * eyes give it a distinct, friendly-but-cunning read. Fantasy spirit, not a literal fox (one
 * coherent entity, FR-1.9); night-themed twilight-blue body with warm amber accents (A57: colour
 * only supports, posture and ear/tail motion carry the character).
 */

import type { PetVariant } from './types';

export const fox: PetVariant = {
  id: 'fox',
  name: 'Fox-Spirit',
  description: 'Fuchsohriger Zwielicht-Geist — wach, warm, leicht verschmitzt, mit buschigem Schwanz.',
  bodyWidth: 1.05,
  bodyHeight: 1.0,
  earShape: 'fox-ears',
  earSize: 1.0,
  tailShape: 'fluffy',
  tailLength: 1.0,
  eyeStyle: 'crescent',
  eyeSize: 1.0,
  outlineWeight: 1.5,
  idleMotionAmplitude: 1.1,
  palette: {
    bodyHue: 266,
    bodySat: 0.55,
    bodyLightOnLight: 0.4,
    bodyLightOnDark: 0.62,
    accentHue: 34,
  },
};
