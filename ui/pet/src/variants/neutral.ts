/**
 * neutral: the existing abstract placeholder blob — no ears, no horns, no tail, round eyes,
 * a plain violet tint. Not one of the concepts; kept only as the safe default until a final design
 * is chosen from the four variants below ("default is `neutral` until chosen").
 */

import type { PetVariant } from './types';

export const neutral: PetVariant = {
  id: 'neutral',
  name: 'Neutral (Platzhalter)',
  description:
    'Abstrakter Platzhalter ohne Ohren, Horn oder Schwanz — kein Konzeptentwurf, nur der bisherige Referenzkörper.',
  bodyWidth: 1,
  bodyHeight: 1,
  earShape: 'none',
  earSize: 0,
  tailShape: 'none',
  tailLength: 0,
  eyeStyle: 'round',
  eyeSize: 1,
  outlineWeight: 0,
  idleMotionAmplitude: 1,
  palette: {
    bodyHue: 255,
    bodySat: 0.62,
    bodyLightOnLight: 0.42,
    bodyLightOnDark: 0.68,
    accentHue: 255,
  },
};
