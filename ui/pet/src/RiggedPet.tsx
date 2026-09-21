/**
 * The rigged renderer: one canvas, one animation loop, a deformation rig underneath.
 *
 * Chosen over `SpritePet` whenever the variant ships a `rig.json` that loads. Same props, same
 * transparent window, same state machine feeding it — so the two are interchangeable and a rig
 * failure is a one-line fallback in `App.tsx` rather than a different pet.
 *
 * Nothing about a frame goes through React state. The state machine's output is written to a ref
 * and read by the loop, so a pet that is breathing, blinking and turning its head re-renders the
 * component exactly never; the only React state here is the accessible label, which changes when
 * the pet does something a person would describe differently.
 *
 * Three things switch the loop off rather than slowing it down: `prefers-reduced-motion` (breathing
 * only, no unprompted motion), a hidden window (nothing at all, and the clock is rebased on return
 * so the creature does not jump), and `?still=1` (exactly one frame, for screenshots).
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import type { AnimParams, PetInput } from './petState';
import { AmbientScheduler, seededRandom } from './rig/ambient';
import { createRigRenderer } from './rig/renderer';
import type { LoadedRig } from './rig/rigFile';
import { RigScene } from './rig/scene';
import { isRestingState, planFor } from './rig/stateMapping';
import type { SpriteState } from './variants/sprite';

export interface RiggedPetProps {
  rig: LoadedRig;
  input: PetInput;
  /** Only `fps`, `paused` and `label` are used; the rig reads the rest from `input` itself. */
  params: AnimParams;
  /** Same projection the sprite path uses, purely for the accessible label. */
  expression: SpriteState;
  interactive: boolean;
  onInteract: (type: 'click', x: number, y: number) => void;
  size?: number;
  still?: boolean;
  /** Dev preview (`?still=1` / `?animate=1`): fix the ambient schedule so two review renders of
   * the same rig can be compared frame for frame. */
  preview?: boolean;
  petLabel?: string;
  /** Called once with the chosen backend, so a WebGL downgrade is reported rather than hidden. */
  onBackend?: (backend: 'webgl' | 'canvas2d', downgradeReason: string | null) => void;
}

/** Key pose to cross-dissolve to per state, once the art for it exists (`rig.json: poses`). */
const POSE_FOR_STATE: Readonly<Record<string, string>> = {
  sleeping: 'curl',
  listening: 'sit',
  speaking: 'sit',
};

/** Fixed seed for the preview modes. Any value; what matters is that it never changes. */
const PREVIEW_SEED = 0x4e6f78;

/** Functional states the creature flinches into: the kill switch stopping it, and a hard error. */
const STARTLING: ReadonlySet<string> = new Set(['unavailable', 'error']);

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  );
}

export function RiggedPet({
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
  onBackend,
}: RiggedPetProps) {
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

    let scene: RigScene;
    try {
      const renderer = createRigRenderer(canvas);
      onBackend?.(renderer.backend, renderer.downgradeReason);
      scene = new RigScene(rig, renderer);
    } catch (error) {
      // Nothing to draw with. `App.tsx` keeps the sprite path mounted behind this, so the pet is
      // still there; what is lost is the motion, and the reason for that is said out loud.
      console.warn('pet.rig_renderer_unavailable', error instanceof Error ? error.message : error);
      return;
    }
    if (scene.missingPoses.length > 0) {
      console.info('pet.rig_poses_missing', scene.missingPoses.join(', '));
    }

    const reduced = prefersReducedMotion();
    const ambient = new AmbientScheduler(preview ? seededRandom(PREVIEW_SEED) : Math.random);
    scene.resize(size, window.devicePixelRatio || 1);

    let active: readonly string[] = [];
    let lastFunctional = inputRef.current.functional;
    const applyPlan = (nowMs: number): number => {
      const current = inputRef.current;
      const plan = planFor(current, reduced);
      // The kill switch and a hard error are the two things that happen *to* the creature rather
      // than being decided by it, and they are the only things it flinches at.
      if (current.functional !== lastFunctional) {
        if (STARTLING.has(current.functional) && scene.player.has('startle')) {
          scene.player.play('startle', nowMs, { fadeMs: 0, restart: true });
        }
        lastFunctional = current.functional;
      }
      const wanted = plan.sustained.filter((clip) => scene.player.has(clip.name));
      const names = new Set<string>(wanted.map((clip) => clip.name));
      for (const clip of wanted) {
        scene.player.play(clip.name, nowMs, { weight: clip.weight });
      }
      // One-shots (blink, ear flick, startle) must survive a plan change, so only clips this plan
      // stopped asking for are faded out.
      for (const running of active) {
        if (!names.has(running)) scene.player.stop(running);
      }
      active = [...names];
      scene.player.setPosture(plan.posture);
      ambient.update(nowMs, plan.ambient, scene.player);
      scene.setPose(POSE_FOR_STATE[expressionOf(current)] ?? null, nowMs);
      return plan.lidClosure;
    };

    const startMs = performance.now();
    ambient.reset(startMs);
    let lidClosure = applyPlan(startMs);
    scene.player.update(startMs);
    scene.frame(startMs, lidClosure);

    if (still) {
      return () => scene.dispose();
    }

    let lastFrameMs = 0;
    let raf = requestAnimationFrame(function loop(nowMs: number) {
      raf = requestAnimationFrame(loop);
      if (paramsRef.current.paused || document.hidden) return;
      // Same frame policy the other two renderers honour: the sleep tiers really do throttle.
      const minimumGapMs = 1000 / paramsRef.current.fps;
      if (nowMs - lastFrameMs < minimumGapMs) return;
      lastFrameMs = nowMs;
      lidClosure = applyPlan(nowMs);
      scene.frame(nowMs, lidClosure);
    });

    const onVisibility = (): void => {
      if (document.hidden) return;
      // A window that was hidden for an hour must not replay an hour of breathing in one frame.
      const nowMs = performance.now();
      scene.player.resetClock(nowMs);
      ambient.reset(nowMs);
      lastFrameMs = 0;
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener('visibilitychange', onVisibility);
      scene.dispose();
    };
  }, [rig, size, still, preview, onBackend]);

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
  const name = [`Nox`, params.label ?? label, petLabel].filter(Boolean).join(' · ');

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

/** The one word that decides which key pose a state wants. Deliberately coarse. */
function expressionOf(input: PetInput): string {
  if (isRestingState(input)) return 'sleeping';
  return input.functional;
}
