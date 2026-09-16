/**
 * PetVariant: one static procedural concept for the Nox creature (OP-1, Decision Plan 2026-09-11 /
 * Decision Evaluation 2026-09-11 "missing option": static renders + one parameter file each, PO
 * picks from images, only the chosen set is implemented).
 *
 * A variant is silhouette + palette + motion tuning only. It never adds a new renderer: `Pet.tsx`
 * draws every one of the 21 `EXPRESSIONS` (src/nox/core/state.py) for every variant through the
 * same posture/mimicry pipeline (`petState.ts::deriveAnim`). Colour (the palette below) only
 * supports the expression, it never carries it alone (A57/D34) — mimicry and posture (eye
 * openness, brows, mouth curve, tilt, bob, ring shape) do that work regardless of variant.
 */

export type EarShape = 'none' | 'imp-horns' | 'fox-ears' | 'owl-tufts' | 'cat-ears';
export type TailShape = 'none' | 'thin-whip' | 'fluffy' | 'sleek-curl';
export type EyeStyle = 'round' | 'slit' | 'wide-oval' | 'crescent';

export interface PetPalette {
  /** Base hue 0..360 — the creature's identity tint. Expression deltas shift away from this, they
   * never replace it entirely (see EXPRESSION_TABLE `hueShift` in petState.ts). */
  bodyHue: number;
  /** Saturation 0..1. */
  bodySat: number;
  /** Body lightness 0..1 tuned for contrast against a pale/light host background (light theme). */
  bodyLightOnLight: number;
  /** Body lightness 0..1 tuned for contrast against a near-black host background (dark theme,
   * the default for the pet window / OBS overlay). This is what the renderer actually uses today
   * (Pet.tsx has no theme switch yet); both values are validated for contrast (see variants tests). */
  bodyLightOnDark: number;
  /** Hue used for ear/tail/marking accents, distinct from the body tint. */
  accentHue: number;
}

export interface PetVariant {
  /** Stable id, used as the `?variant=` query value and the PNG/asset filename prefix. */
  id: string;
  /** Display name for the PRD gallery note. */
  name: string;
  /** One-line character description (also used verbatim in the OP-1 gallery note). */
  description: string;
  /** Ellipse half-width multiplier relative to the base radius. */
  bodyWidth: number;
  /** Ellipse half-height multiplier relative to the base radius. */
  bodyHeight: number;
  earShape: EarShape;
  /** 0..1, ignored when earShape is 'none'. */
  earSize: number;
  tailShape: TailShape;
  /** 0..1, ignored when tailShape is 'none'. */
  tailLength: number;
  eyeStyle: EyeStyle;
  /** 0..1 relative eye scale on top of the eyeStyle's base proportions. */
  eyeSize: number;
  /** Outline stroke width in px at the 260px reference size; 0 draws no outline. */
  outlineWeight: number;
  /** Multiplier on the idle breathing/bob/jitter amplitude (character energy level). */
  idleMotionAmplitude: number;
  palette: PetPalette;
}
