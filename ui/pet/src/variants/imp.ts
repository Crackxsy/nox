/**
 * imp: a small horned imp-like nightling — quick, mischievous, always a little too pleased with
 * itself. Curved ember-tipped horns, a thin whip tail with an arrow tip, narrow slit eyes and a
 * lean body read as "impish" at a glance without any generic "AI mascot" roundness (A73). Fantasy,
 * night-themed (deep magenta-violet with ember-orange accents), one coherent entity — no separate
 * parts are swappable at runtime (D34/FR-1.9).
 */

import type { PetVariant } from './types';

export const imp: PetVariant = {
  id: 'imp',
  name: 'Imp',
  description: 'Kleiner gehörnter Unruhestifter — schnell, frech, mit schmalen Augen und Peitschenschwanz.',
  bodyWidth: 0.85,
  bodyHeight: 0.95,
  earShape: 'imp-horns',
  earSize: 0.85,
  tailShape: 'thin-whip',
  tailLength: 0.95,
  eyeStyle: 'slit',
  eyeSize: 0.9,
  outlineWeight: 2,
  idleMotionAmplitude: 1.3,
  palette: {
    bodyHue: 320,
    bodySat: 0.58,
    bodyLightOnLight: 0.38,
    bodyLightOnDark: 0.6,
    accentHue: 18,
  },
};
