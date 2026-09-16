/**
 * neutral: the existing abstract placeholder blob (D31) — no ears, no horns, no tail, round eyes,
 * a plain violet tint. Not an OP-1 concept; kept only as the safe default until the PO picks one of
 * the four variants below, per the Decision Plan 2026-09-11 OP-1 instruction "quality: default is
 * `neutral` until chosen".
 */

import type { PetVariant } from './types';

export const neutral: PetVariant = {
  id: 'neutral',
  name: 'Neutral (Platzhalter)',
  description:
    'Abstrakter Platzhalter ohne Ohren, Horn oder Schwanz — kein OP-1-Konzept, nur der bisherige Referenzkörper.',
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
