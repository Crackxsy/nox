/**
 * Sprite renderer (#19). Plays a `SpriteVariant`'s pre-rendered frames instead of drawing the pet
 * procedurally, and is otherwise interchangeable with `Pet.tsx`: same props, same transparent
 * window, same state machine feeding it.
 *
 * Why DOM and not canvas: the frames are already finished art with alpha. Two stacked <img>
 * elements give a correct crossfade, correct alpha compositing against the transparent window, and
 * `prefers-reduced-motion` handling for free — all of which would have to be hand-rolled on a
 * canvas. The 150 ms crossfade and the idle breathing are CSS (`styles.css`); this file only
 * decides *which* frame is up.
 *
 * Frame updates go through refs and direct `src`/`data-active` writes rather than React state: a
 * sequence at 8 fps would otherwise re-render the tree eight times a second for no benefit. React
 * state is used only for the accessible label, which changes once per expression.
 */

import { useEffect, useRef, useState } from 'react';

import type { AnimParams } from './petState';
import {
  CROSSFADE_MS,
  type LoadedSprite,
  type SpriteExpression,
  type SpriteState,
  frameIndexAt,
  framesFor,
} from './variants/sprite';

export interface SpritePetProps {
  sprite: LoadedSprite;
  /** Expression chosen by `spriteExpressionFor` from the live pet state. */
  expression: SpriteState;
  /** Only `label` is used — the status word the procedural renderer draws under the creature. */
  params: AnimParams;
  interactive: boolean;
  onInteract: (type: 'click', x: number, y: number) => void;
  size?: number;
  /** Freeze on the first frame of the current expression (`?still=1` screenshots). */
  still?: boolean;
  /** Accessible name of the creature control, from the app's language table. */
  petLabel?: string;
}

/** A blink is a cut, not a crossfade: these are the milliseconds it stays down. */
const BLINK_MS = 140;
/** Every 4–7 s, jittered: a fixed rhythm reads as a machine, a jittered one reads as alive. */
const BLINK_MIN_GAP_MS = 4000;
const BLINK_EXTRA_GAP_MS = 3000;
/** Expressions a blink may interrupt. Sleeping eyes are already shut; a talking face is busy. */
const BLINKABLE: ReadonlySet<SpriteState> = new Set<SpriteState>(['idle', 'listening', 'thinking']);

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

/** Browser `ImageLoader`: resolves when the frame has decoded, rejects when it never will. */
export function loadImageElement(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.decoding = 'async';
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`cannot load ${url}`));
    img.src = url;
  });
}

export function SpritePet({
  sprite,
  expression,
  params,
  interactive,
  onInteract,
  size = 260,
  still = false,
  petLabel,
}: SpritePetProps) {
  const slotA = useRef<HTMLImageElement | null>(null);
  const slotB = useRef<HTMLImageElement | null>(null);
  const expressionRef = useRef<SpriteState>(expression);
  const fpsRef = useRef(params.fps);
  const pausedRef = useRef(params.paused);
  const [label, setLabel] = useState<SpriteExpression>(expression);

  // Refs are written in effects, not during render: React reserves the render phase for pure work
  // and StrictMode double-invokes it.
  useEffect(() => {
    expressionRef.current = expression;
  }, [expression]);

  useEffect(() => {
    fpsRef.current = params.fps;
    pausedRef.current = params.paused;
  }, [params.fps, params.paused]);

  useEffect(() => {
    const a = slotA.current;
    const b = slotB.current;
    if (!a || !b) return;
    const { manifest, urls } = sprite;
    const reduced = prefersReducedMotion();

    let active: HTMLImageElement = a;
    let idle: HTMLImageElement = b;
    let shownExpression: SpriteExpression | null = null;
    let shownFile: string | null = null;
    let sequenceStart = performance.now();
    let blinkAt = sequenceStart + BLINK_MIN_GAP_MS + Math.random() * BLINK_EXTRA_GAP_MS;
    let blinkUntil = 0;

    const show = (file: string, crossfade: boolean): void => {
      if (file === shownFile) return;
      if (crossfade && !reduced && shownFile !== null) {
        idle.src = urls[file] ?? file;
        idle.dataset.active = 'true';
        active.dataset.active = 'false';
        [active, idle] = [idle, active];
      } else {
        active.src = urls[file] ?? file;
        active.dataset.active = 'true';
        idle.dataset.active = 'false';
      }
      shownFile = file;
    };

    const tick = (now: number): void => {
      let wanted: SpriteExpression = expressionRef.current;
      const canBlink = manifest.frames.blink !== undefined && BLINKABLE.has(expressionRef.current);
      if (canBlink && !reduced && !still) {
        if (now >= blinkAt) {
          blinkUntil = now + BLINK_MS;
          blinkAt = now + BLINK_MIN_GAP_MS + Math.random() * BLINK_EXTRA_GAP_MS;
        }
        if (now < blinkUntil) wanted = 'blink';
      }

      const { expression: resolved, files } = framesFor(manifest, wanted);
      const changed = resolved !== shownExpression;
      if (changed) {
        sequenceStart = now;
        shownExpression = resolved;
        if (resolved !== 'blink') setLabel(resolved);
      }
      const index = still ? 0 : frameIndexAt(files, manifest.fps, now - sequenceStart);
      // A blink is a cut; every other expression change crossfades (#19: 150 ms).
      show(files[index], changed && resolved !== 'blink' && wanted !== 'blink');
    };

    tick(performance.now());
    if (still) return;
    // The sprite loop honours the same frame policy the procedural renderer does (`fpsFor`, A146):
    // it used to tick on every animation frame regardless, so the 10/15/30/60 fps sleep tiers had
    // no effect at all on a sprite variant.
    let lastTick = 0;
    let raf = requestAnimationFrame(function loop(now: number) {
      raf = requestAnimationFrame(loop);
      if (pausedRef.current) return;
      if (now - lastTick < 1000 / fpsRef.current) return;
      lastTick = now;
      tick(now);
    });
    return () => cancelAnimationFrame(raf);
  }, [sprite, still]);

  const box = size * sprite.manifest.scale;
  const [ax, ay] = sprite.manifest.anchor;
  // Positioning lives here and only here. `.pet-sprite-frame` used to also declare `inset: 0` and
  // `width/height: 100%`, all three of which these values override — three dead declarations that
  // read like the layout rule but were not.
  const frameStyle: React.CSSProperties = {
    left: size / 2 - ax * box,
    top: size / 2 - ay * box,
    width: box,
    height: box,
  };

  const creature = (
    <div
      className="pet-sprite"
      data-state={label}
      style={{
        width: size,
        height: size,
        ['--pet-crossfade-ms' as string]: `${CROSSFADE_MS}ms`,
      }}
    >
      <img ref={slotA} className="pet-sprite-frame" style={frameStyle} alt="" data-active="false" />
      <img ref={slotB} className="pet-sprite-frame" style={frameStyle} alt="" data-active="false" />
    </div>
  );

  const name = [`Nox`, params.label ?? label, petLabel].filter(Boolean).join(' · ');

  if (!interactive) {
    return (
      <div role="img" aria-label={name}>
        {creature}
      </div>
    );
  }

  // The creature is a control, so it is a <button>: `pet.interact` was mouse-only, which left the
  // pet's single interaction unreachable from the keyboard.
  return (
    <button
      type="button"
      className="pet-hit"
      aria-label={name}
      onClick={(e) => {
        const rect = e.currentTarget.getBoundingClientRect();
        // A keyboard activation reports (0, 0); answer with the centre instead of a corner.
        const centred = e.clientX === 0 && e.clientY === 0;
        onInteract(
          'click',
          Math.round(centred ? rect.width / 2 : e.clientX - rect.left),
          Math.round(centred ? rect.height / 2 : e.clientY - rect.top),
        );
      }}
    >
      {creature}
    </button>
  );
}
