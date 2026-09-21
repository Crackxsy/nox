/**
 * fox: a fox-eared spirit of the dusk — alert, warm, a little sly. Tall pointed ears with pale
 * inner markings, a fluffy tail tipped in moonlight and softly narrowed "grinning" crescent eyes
 * give it a distinct, friendly-but-cunning read. Fantasy spirit, not a literal fox (one coherent
 * entity, FR-1.9).
 *
 * Amber-rust body with cool moonlight accents. The four concepts used to sit at hue 248–320, i.e.
 * five shades of violet that differed only by ear and tail silhouette; at 220 px on a dark desktop
 * that is not four creatures, it is one creature four times. Each concept now owns a family of
 * colour as well as a shape (A57 still holds: colour *supports*, posture and ear/tail motion carry
 * the character).
 */

import type { PetVariant } from './types';

export const fox: PetVariant = {
  id: 'fox',
  name: 'Fuchsgeist',
  description: 'Fuchsohriger Dämmerungsgeist — wach, warm, leicht verschmitzt, mit buschigem Schwanz.',
  bodyWidth: 1.05,
  bodyHeight: 1.0,
  earShape: 'fox-ears',
  // Ears and tail are the whole silhouette at 220 px; at 1.0 they read as two small dark triangles.
  earSize: 1.3,
  tailShape: 'fluffy',
  tailLength: 0.9,
  eyeStyle: 'crescent',
  eyeSize: 1.0,
  outlineWeight: 1.5,
  idleMotionAmplitude: 1.1,
  palette: {
    bodyHue: 24,
    bodySat: 0.62,
    bodyLightOnLight: 0.4,
    bodyLightOnDark: 0.62,
    accentHue: 202,
  },
};
