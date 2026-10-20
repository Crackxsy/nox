/**
 * The relief renderer as a pet: the painted creature moved by a skeleton with spring physics and
 * turned in 3D by its relief, driven by the same state machine as the other renderers.
 *
 * Same props and same transparent canvas as `RiggedPet`, so `App.tsx` can swap one for the other.
 * Nothing about a frame goes through React state: the state machine's output lands in refs and
 * the animation loop reads them.
 *
 * What the front view cannot do by deformation - curled up asleep, lying down, eating - stays the
 * key-pose art from `rig.json`; `PoseSwitch` hides the change of picture under a squash. Falling
 * asleep is first the relief creature nodding off, and only then the curl.
 *
 * `prefers-reduced-motion`: breathing, held posture and the jaw only; pose changes are a short
 * dissolve. A hidden window draws nothing and rebases the clock on return. `?still=1` draws one
 * frame. Any setup failure (no WebGL2, a broken rig) calls `onUnavailable` and `App.tsx` falls back
 * to the older renderer.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import type { AnimParams, PetInput } from './petState';
import { type Face, ReliefBrain } from './relief/brain';
import { type Pixels, buildGeometry } from './relief/mesh';
import type { LoadedRelief } from './relief/reliefFile';
import { ReliefRenderer } from './relief/renderer';
import { Skeleton } from './relief/skeleton';
import { EATING_POSE, poseFor, reliefTargets } from './relief/stateMapping';
import { PoseSwitch } from './relief/transition';
import type { LoadedRig } from './rig/rigFile';
import { isRestingState } from './rig/stateMapping';
import type { SpriteState } from './variants/sprite';

export interface ReliefPetProps {
  relief: LoadedRelief;
  /** The older rig: its key-pose drawings are reused for curl / lie / eat. */
  rig: LoadedRig;
  input: PetInput;
  /** `fps`, `paused` and `label`, plus the expression parameters the relief mapping reads. */
  params: AnimParams;
  expression: SpriteState;
  interactive: boolean;
  onInteract: (type: 'click' | 'feed', x: number, y: number) => void;
  size?: number;
  still?: boolean;
  /** Dev preview: fixed random seed, and frame timings published for the measuring script. */
  preview?: boolean;
  petLabel?: string;
  /** The renderer cannot run here; the caller switches to its fallback. */
  onUnavailable: (reason: string) => void;
}

/** How long the creature stays on the eating pose after a treat. */
const EATING_MS = 2600;
/** Asleep: nod off as the relief creature first, then curl up. */
const CURL_AFTER_MS = 2400;
const PREVIEW_SEED = 42;
/** Functional states the creature flinches into: the kill switch, and a hard error. */
const STARTLING: ReadonlySet<string> = new Set(['unavailable', 'error']);

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

function readPixels(img: TexImageSource): Pixels {
  const w = 'naturalWidth' in img ? img.naturalWidth : (img as { width: number }).width;
  const h = 'naturalHeight' in img ? img.naturalHeight : (img as { height: number }).height;
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  const g = c.getContext('2d', { willReadFrequently: true });
  if (!g) throw new Error('no 2D canvas to read the rig textures');
  g.drawImage(img as CanvasImageSource, 0, 0);
  return { data: g.getImageData(0, 0, w, h).data, width: w, height: h };
}

/** Frame timings published in preview mode (`window.__noxRelief`), read by the measuring script. */
interface ReliefStats {
  frames: number;
  /** JS time per drawn frame, ms, ring buffer of the last 600. */
  jsMs: number[];
  vertices: number;
  /** The face of the last frame (lids, mouth, head turn), for the measuring script. */
  face?: unknown;
  /** `functional/expression` of the last frame. */
  state?: string;
}

export function ReliefPet({
  relief,
  rig,
  input,
  params,
  expression,
  interactive,
  onInteract,
  size = 260,
  still = false,
  preview = false,
  petLabel,
  onUnavailable,
}: ReliefPetProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const inputRef = useRef(input);
  const paramsRef = useRef(params);
  const [label, setLabel] = useState<SpriteState>(expression);

  useEffect(() => {
    inputRef.current = input;
  }, [input]);
  useEffect(() => {
    paramsRef.current = params;
  }, [params]);
  useEffect(() => setLabel(expression), [expression]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const def = relief.definition;
    let renderer: ReliefRenderer;
    let skel: Skeleton;
    try {
      skel = new Skeleton(def.bones);
      const geo = buildGeometry(def, skel, readPixels(relief.atlas), readPixels(relief.depth));
      renderer = new ReliefRenderer(canvas, def, geo, skel, relief.atlas);
      if (preview) {
        (window as unknown as { __noxRelief: ReliefStats }).__noxRelief = {
          frames: 0,
          jsMs: [],
          vertices: geo.vertexCount,
        };
      }
    } catch (error) {
      onUnavailable(error instanceof Error ? error.message : String(error));
      return;
    }
    const reduced = prefersReducedMotion();
    const brain = new ReliefBrain(def, preview ? PREVIEW_SEED : (Math.random() * 2 ** 31) | 0);
    const poses = new PoseSwitch();
    renderer.resize(size, window.devicePixelRatio || 1);

    const poseImage = (name: string | null): TexImageSource | null => {
      if (name === null) return null;
      const frame = rig.definition.poses[name];
      return frame ? (rig.images[frame.file] ?? null) : null;
    };

    let startMs = performance.now();
    let lastMs = startMs;
    let lastFunctional = inputRef.current.functional;
    let lastFeeds = inputRef.current.feeds;
    let eatingUntilMs = 0;
    let restingSinceMs: number | null = null;

    const draw = (nowMs: number, first: boolean): Face => {
      const current = inputRef.current;
      const t = (nowMs - startMs) / 1000;
      const dt = first ? 0 : Math.min(0.1, (nowMs - lastMs) / 1000);
      lastMs = nowMs;
      if (current.functional !== lastFunctional) {
        if (STARTLING.has(current.functional) && !reduced) brain.startle(t);
        lastFunctional = current.functional;
      }
      if (current.feeds !== lastFeeds) {
        lastFeeds = current.feeds;
        eatingUntilMs = nowMs + EATING_MS;
      }
      const resting = isRestingState(current);
      if (resting && restingSinceMs === null) restingSinceMs = nowMs;
      if (!resting) restingSinceMs = null;
      let wanted = nowMs < eatingUntilMs ? EATING_POSE : poseFor(current);
      if (
        wanted === 'curl' &&
        !first &&
        !reduced &&
        restingSinceMs !== null &&
        nowMs - restingSinceMs < CURL_AFTER_MS
      ) {
        wanted = null;
      }
      if (poseImage(wanted) === null) wanted = null; // no art for it: stay the creature, honestly
      if (first) poses.set(wanted);
      const sw = poses.update(nowMs, wanted, reduced);

      const targets = reliefTargets(current, paramsRef.current, reduced);
      if (poses.involves('curl')) {
        // still asleep while curled up or uncurling; the brain wakes (yawn) once it is the creature
        targets.asleep = 1;
        targets.lid = 1;
      }
      const face = brain.update(t, dt, skel, targets);
      renderer.frame({
        face,
        squash: sw.squash,
        pose: poseImage(sw.pose),
        poseScale: (sw.pose && def.poses[sw.pose]?.scale) || 1,
        poseAlpha: sw.poseAlpha,
        reliefVisible: sw.reliefVisible,
      });
      if (renderer.contextLost) onUnavailable('WebGL2 context lost');
      return face;
    };

    draw(startMs, true);
    if (still) return () => renderer.dispose();

    let lastFrameMs = 0;
    const stats = preview ? (window as unknown as { __noxRelief: ReliefStats }).__noxRelief : null;
    let raf = requestAnimationFrame(function loop(nowMs: number) {
      raf = requestAnimationFrame(loop);
      if (paramsRef.current.paused || document.hidden) return;
      // the sleep tiers throttle this loop like the other renderers
      if (nowMs - lastFrameMs < 1000 / paramsRef.current.fps - 1) return;
      lastFrameMs = nowMs;
      const t0 = performance.now();
      const face = draw(nowMs, false);
      if (stats) {
        stats.face = face;
        stats.state = `${inputRef.current.functional}/${inputRef.current.expression}`;
        stats.frames += 1;
        stats.jsMs.push(performance.now() - t0);
        if (stats.jsMs.length > 600) stats.jsMs.shift();
      }
    });

    const onVisibility = (): void => {
      if (document.hidden) return;
      // an hour hidden must not replay an hour of breathing in one frame
      const nowMs = performance.now();
      startMs += nowMs - lastMs;
      lastMs = nowMs;
      lastFrameMs = 0;
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener('visibilitychange', onVisibility);
      renderer.dispose();
    };
  }, [relief, rig, size, still, preview, onUnavailable]);

  const handleClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      const rect = event.currentTarget.getBoundingClientRect();
      const centred = event.clientX === 0 && event.clientY === 0;
      onInteract(
        'click',
        Math.round(centred ? rect.width / 2 : event.clientX - rect.left),
        Math.round(centred ? rect.height / 2 : event.clientY - rect.top),
      );
    },
    [onInteract],
  );

  const creature = (
    <canvas
      ref={canvasRef}
      className="pet-rig-canvas"
      style={{ width: size, height: size }}
      aria-hidden="true"
    />
  );
  const name = ['Nox', params.label ?? label, petLabel].filter(Boolean).join(' · ');

  if (!interactive) {
    return (
      <div className="pet-rig" role="img" aria-label={name}>
        {creature}
      </div>
    );
  }
  return (
    <button type="button" className="pet-hit" aria-label={name} onClick={handleClick}>
      <span className="pet-rig">{creature}</span>
    </button>
  );
}
