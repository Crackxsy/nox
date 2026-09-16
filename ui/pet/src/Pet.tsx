/**
 * Procedural Canvas 2D renderer. Draws one coherent fantasy creature (OP-1, D34) from AnimParams
 * plus a PetVariant silhouette (body proportions, ears, tail, eye style, outline, palette, idle
 * motion amplitude). All 21 expressions are expressed through posture/mimicry parameters coming
 * from AnimParams (petState.ts::deriveAnim) — the variant only fixes *which creature* is doing the
 * expressing, never how an expression itself looks; colour (palette) only supports it (A57/D34).
 */

import { useEffect, useRef } from 'react';

import { type AnimParams, decaySpeaking } from './petState';
import { neutral } from './variants';
import type { PetVariant } from './variants';

export interface PetProps {
  params: AnimParams;
  /** Current speaking level; the renderer decays it between events for a smooth pulse. */
  speakingLevel: number;
  onSpeakingDecay: (level: number) => void;
  interactive: boolean;
  onInteract: (type: 'click', x: number, y: number) => void;
  size?: number;
  /** Silhouette/palette/motion parameters (OP-1); defaults to the `neutral` placeholder. */
  variant?: PetVariant;
}

const TAU = Math.PI * 2;

export function Pet({
  params,
  speakingLevel,
  onSpeakingDecay,
  interactive,
  onInteract,
  size = 260,
  variant = neutral,
}: PetProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const paramsRef = useRef(params);
  const variantRef = useRef(variant);
  const levelRef = useRef(speakingLevel);
  paramsRef.current = params;
  variantRef.current = variant;
  levelRef.current = Math.max(levelRef.current, speakingLevel);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    let raf = 0;
    let last = performance.now();
    let lastDraw = 0;
    let blinkAt = last + 2500 + Math.random() * 3000;
    let blink = 0;

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const p = paramsRef.current;
      const minInterval = 1000 / p.fps;
      if (now - lastDraw < minInterval) return;
      const dt = now - last;
      last = now;
      lastDraw = now;

      if (levelRef.current > 0) {
        levelRef.current = decaySpeaking(levelRef.current, dt);
        if (levelRef.current === 0) onSpeakingDecay(0);
      }
      if (now > blinkAt) {
        blink = 1;
        blinkAt = now + 2500 + Math.random() * 4000;
      }
      blink = Math.max(0, blink - dt / 120);
      draw(ctx, p, variantRef.current, now / 1000, size, levelRef.current, blink);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [size, onSpeakingDecay]);

  const handleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    const rect = e.currentTarget.getBoundingClientRect();
    onInteract('click', Math.round(e.clientX - rect.left), Math.round(e.clientY - rect.top));
  };

  return (
    <canvas
      ref={canvasRef}
      style={{ width: size, height: size, display: 'block', cursor: interactive ? 'pointer' : 'default' }}
      role="img"
      aria-label={`Nox ${params.label ?? ''}`.trim()}
      onClick={handleClick}
    />
  );
}

function hsl(h: number, s: number, l: number, a = 1): string {
  return `hsla(${h.toFixed(0)}, ${(s * 100).toFixed(0)}%, ${(l * 100).toFixed(0)}%, ${a})`;
}

function draw(
  ctx: CanvasRenderingContext2D,
  p: AnimParams,
  variant: PetVariant,
  t: number,
  size: number,
  level: number,
  blink: number,
) {
  ctx.clearRect(0, 0, size, size);
  const cx = size / 2;
  const cy = size * 0.56;
  const r = size * 0.27;
  const amp = variant.idleMotionAmplitude;

  const breath = 1 + p.breathAmp * amp * Math.sin(t * TAU * p.breathRate);
  const bob = p.bob * amp * Math.sin(t * TAU * 1.6) * size * 0.02;
  const jx = p.jitter * amp * (Math.random() - 0.5) * 2;
  const jy = p.jitter * amp * (Math.random() - 0.5) * 2;

  const body = hsl(p.hue, p.sat, p.light);
  const accent = hsl(variant.palette.accentHue, p.sat, Math.max(0.25, p.light - 0.14));
  const dark = '#0b0b12';

  ctx.save();
  ctx.translate(cx + jx, cy + bob + jy);
  ctx.rotate(p.tilt);

  // glow
  if (p.glow > 0) {
    const g = ctx.createRadialGradient(0, 0, r * 0.6, 0, 0, r * 1.7);
    g.addColorStop(0, hsl(p.hue, p.sat, p.light, p.glow * 0.5));
    g.addColorStop(1, hsl(p.hue, p.sat, p.light, 0));
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(0, 0, r * 1.7, 0, TAU);
    ctx.fill();
  }

  const bw = r * 1.15 * variant.bodyWidth;
  const bh = r * variant.bodyHeight;

  // tail (behind the body)
  drawTail(ctx, variant, t, r, bw, bh, amp, body, accent);

  // body (squash/stretch breathing) + ears + eyes + mouth, all inside the same breath scale
  ctx.save();
  ctx.scale(breath, 1 / breath);

  drawEars(ctx, variant, bw, bh, dark, accent);

  ctx.fillStyle = body;
  ctx.beginPath();
  ctx.ellipse(0, 0, bw, bh, 0, 0, TAU);
  ctx.fill();
  if (variant.outlineWeight > 0) {
    ctx.lineWidth = variant.outlineWeight;
    ctx.strokeStyle = dark;
    ctx.stroke();
  }

  // eyes
  const eyeY = -bh * 0.15 - p.eyeLift * bh * 0.25;
  const eyeDx = bw * 0.34 * p.eyeSpread;
  const open = Math.max(0.02, p.eyeOpen * (1 - blink));
  drawEyes(ctx, p, variant, r, eyeDx, eyeY, open, dark);

  // mouth
  const my = bh * 0.35;
  const mw = bw * 0.28;
  ctx.strokeStyle = dark;
  ctx.lineWidth = 3;
  ctx.lineCap = 'round';
  const mouthOpen = Math.max(p.mouthOpen, level);
  if (mouthOpen > 0.05) {
    ctx.fillStyle = dark;
    ctx.beginPath();
    ctx.ellipse(0, my, mw * 0.7, r * 0.22 * mouthOpen, 0, 0, TAU);
    ctx.fill();
  } else {
    ctx.beginPath();
    ctx.moveTo(-mw, my);
    ctx.quadraticCurveTo(0, my + p.mouthCurve * r * 0.35, mw, my);
    ctx.stroke();
  }
  if (p.ring === 'cross') {
    ctx.strokeStyle = '#e05a5a';
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.moveTo(-mw * 1.3, my - r * 0.18);
    ctx.lineTo(mw * 1.3, my + r * 0.18);
    ctx.stroke();
  }
  ctx.restore(); // breath scale

  // status ring (shape, not only colour — D238)
  drawRing(ctx, p, t, r);
  ctx.restore();

  if (p.label) {
    ctx.font = `${Math.round(size * 0.05)}px "Segoe UI", system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.fillStyle = 'rgba(11,11,18,0.85)';
    const tw = ctx.measureText(p.label).width + 16;
    ctx.beginPath();
    ctx.roundRect(cx - tw / 2, size * 0.9, tw, size * 0.075, 8);
    ctx.fill();
    ctx.fillStyle = '#c8c8d4';
    ctx.fillText(p.label, cx, size * 0.9 + size * 0.053);
  }
}

/** Eye style is part of the variant's identity (D34: mimicry carries the expression regardless of
 * style); `open`/`pupilSize`/`browAngle` etc. keep coming from AnimParams so every expression still
 * reads correctly no matter which of the four styles is drawn. */
function drawEyes(
  ctx: CanvasRenderingContext2D,
  p: AnimParams,
  variant: PetVariant,
  r: number,
  eyeDx: number,
  eyeY: number,
  open: number,
  dark: string,
) {
  const scale = variant.eyeSize;
  const eyeW = r * 0.15 * scale * (variant.eyeStyle === 'wide-oval' ? 1.35 : variant.eyeStyle === 'slit' ? 0.85 : 1);
  const eyeHBase = r * 0.24 * scale * (variant.eyeStyle === 'wide-oval' ? 1.25 : 1);
  const eyeH = Math.max(1.5, eyeHBase * open);
  // Crescent is the "sly grin" resting look; only truly wide-eyed states (scared/excited/hype push
  // eyeOpen well past the ~0.85 baseline) break out of it into a normal round, pupil-bearing eye.
  const CRESCENT_OPEN_THRESHOLD = 0.92;
  const isCrescentGrin = variant.eyeStyle === 'crescent' && open < CRESCENT_OPEN_THRESHOLD;

  for (const s of [-1, 1]) {
    const ex = s * eyeDx;
    ctx.fillStyle = dark;
    if (isCrescentGrin) {
      ctx.strokeStyle = dark;
      ctx.lineWidth = Math.max(2, eyeW * 0.5);
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(ex - eyeW, eyeY);
      ctx.quadraticCurveTo(ex, eyeY - eyeH * 1.6, ex + eyeW, eyeY);
      ctx.stroke();
    } else if (variant.eyeStyle === 'slit') {
      ctx.beginPath();
      ctx.ellipse(ex, eyeY, eyeW * 0.45, eyeH, 0, 0, TAU);
      ctx.fill();
    } else {
      ctx.beginPath();
      ctx.ellipse(ex, eyeY, eyeW, eyeH, 0, 0, TAU);
      ctx.fill();
    }
    if (open > 0.3 && !isCrescentGrin) {
      ctx.fillStyle = hsl(p.hue, 0.3, 0.9);
      ctx.beginPath();
      ctx.arc(ex + eyeW * 0.3, eyeY - eyeH * 0.35, eyeW * 0.35 * p.pupilSize, 0, TAU);
      ctx.fill();
    }
    if (Math.abs(p.browAngle) > 0.05) {
      ctx.strokeStyle = dark;
      ctx.lineWidth = 3;
      ctx.beginPath();
      const by = eyeY - eyeH - r * 0.12;
      ctx.moveTo(ex - eyeW * 1.2, by - s * p.browAngle * r * 0.12);
      ctx.lineTo(ex + eyeW * 1.2, by + s * p.browAngle * r * 0.12);
      ctx.stroke();
    }
  }
}

function drawEars(
  ctx: CanvasRenderingContext2D,
  variant: PetVariant,
  bw: number,
  bh: number,
  dark: string,
  accent: string,
): void {
  const size = bh * 0.62 * variant.earSize;
  if (variant.earShape === 'none' || size <= 0) return;
  const baseY = -bh * 0.72;
  for (const s of [-1, 1]) {
    const bx = s * bw * 0.42;
    ctx.save();
    ctx.translate(bx, baseY);
    switch (variant.earShape) {
      case 'imp-horns': {
        ctx.strokeStyle = dark;
        ctx.lineWidth = Math.max(2, size * 0.22);
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(0, size * 0.25);
        ctx.quadraticCurveTo(s * size * 0.7, -size * 0.25, s * size * 0.2, -size * 1.1);
        ctx.stroke();
        ctx.fillStyle = accent;
        ctx.beginPath();
        ctx.arc(s * size * 0.2, -size * 1.1, size * 0.12, 0, TAU);
        ctx.fill();
        break;
      }
      case 'fox-ears': {
        ctx.fillStyle = dark;
        ctx.beginPath();
        ctx.moveTo(-size * 0.45, size * 0.3);
        ctx.lineTo(0, -size * 1.15);
        ctx.lineTo(size * 0.45, size * 0.3);
        ctx.closePath();
        ctx.fill();
        ctx.fillStyle = accent;
        ctx.beginPath();
        ctx.moveTo(-size * 0.22, size * 0.14);
        ctx.lineTo(0, -size * 0.72);
        ctx.lineTo(size * 0.22, size * 0.14);
        ctx.closePath();
        ctx.fill();
        break;
      }
      case 'owl-tufts': {
        ctx.fillStyle = dark;
        ctx.beginPath();
        ctx.moveTo(-size * 0.3, size * 0.3);
        ctx.quadraticCurveTo(-size * 0.15, -size * 0.85, 0, -size * 0.95);
        ctx.quadraticCurveTo(size * 0.15, -size * 0.85, size * 0.3, size * 0.3);
        ctx.closePath();
        ctx.fill();
        break;
      }
      case 'cat-ears': {
        ctx.fillStyle = dark;
        ctx.beginPath();
        ctx.moveTo(-size * 0.4, size * 0.3);
        ctx.lineTo(s * size * 0.05, -size * 1.0);
        ctx.lineTo(size * 0.4, size * 0.3);
        ctx.closePath();
        ctx.fill();
        break;
      }
      default:
        break;
    }
    ctx.restore();
  }
}

function drawTail(
  ctx: CanvasRenderingContext2D,
  variant: PetVariant,
  t: number,
  r: number,
  bw: number,
  bh: number,
  amp: number,
  body: string,
  accent: string,
): void {
  if (variant.tailShape === 'none' || variant.tailLength <= 0) return;
  const len = r * 1.6 * variant.tailLength;
  const wag = Math.sin(t * 1.1) * 0.22 * amp;
  ctx.save();
  ctx.translate(bw * 0.5, bh * 0.5);
  ctx.rotate(0.6 + wag);
  switch (variant.tailShape) {
    case 'thin-whip': {
      ctx.strokeStyle = body;
      ctx.lineWidth = Math.max(2, r * 0.12);
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.quadraticCurveTo(len * 0.5, len * 0.2, len * 0.9, -len * 0.15);
      ctx.stroke();
      ctx.fillStyle = accent;
      ctx.beginPath();
      ctx.moveTo(len * 0.88, -len * 0.22);
      ctx.lineTo(len * 1.05, -len * 0.08);
      ctx.lineTo(len * 0.82, len * 0.02);
      ctx.closePath();
      ctx.fill();
      break;
    }
    case 'fluffy': {
      ctx.fillStyle = body;
      ctx.beginPath();
      ctx.ellipse(len * 0.5, 0, len * 0.55, r * 0.32, 0.3, 0, TAU);
      ctx.fill();
      ctx.fillStyle = accent;
      ctx.beginPath();
      ctx.ellipse(len * 0.88, -r * 0.05, r * 0.22, r * 0.18, 0.3, 0, TAU);
      ctx.fill();
      break;
    }
    case 'sleek-curl': {
      ctx.strokeStyle = body;
      ctx.lineWidth = Math.max(2, r * 0.16);
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.bezierCurveTo(len * 0.5, len * 0.1, len * 0.9, -len * 0.5, len * 0.55, -len * 0.65);
      ctx.stroke();
      break;
    }
    default:
      break;
  }
  ctx.restore();
}

function drawRing(ctx: CanvasRenderingContext2D, p: AnimParams, t: number, r: number) {
  const R = r * 1.45;
  ctx.lineWidth = 3;
  switch (p.ring) {
    case 'none':
      return;
    case 'pulse': {
      const k = (t * 1.2) % 1;
      ctx.strokeStyle = hsl(p.hue, 0.7, 0.75, 1 - k);
      ctx.beginPath();
      ctx.arc(0, 0, R * (0.9 + k * 0.35), 0, TAU);
      ctx.stroke();
      return;
    }
    case 'orbit': {
      for (let i = 0; i < 3; i++) {
        const a = t * 2 + (i * TAU) / 3;
        ctx.fillStyle = hsl(p.hue, 0.6, 0.8);
        ctx.beginPath();
        ctx.arc(Math.cos(a) * R, Math.sin(a) * R * 0.5 - r * 0.9, 4, 0, TAU);
        ctx.fill();
      }
      return;
    }
    case 'segments': {
      ctx.strokeStyle = hsl(p.hue, 0.6, 0.8);
      for (let i = 0; i < 4; i++) {
        const a = t * 1.5 + (i * TAU) / 4;
        ctx.beginPath();
        ctx.arc(0, 0, R, a, a + 0.6);
        ctx.stroke();
      }
      return;
    }
    case 'shield': {
      ctx.strokeStyle = '#5fb37a';
      ctx.beginPath();
      ctx.moveTo(0, -R);
      ctx.lineTo(R * 0.8, -R * 0.5);
      ctx.lineTo(R * 0.6, R * 0.6);
      ctx.lineTo(0, R);
      ctx.lineTo(-R * 0.6, R * 0.6);
      ctx.lineTo(-R * 0.8, -R * 0.5);
      ctx.closePath();
      ctx.stroke();
      return;
    }
    case 'alert': {
      ctx.strokeStyle = '#e05a5a';
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.moveTo(0, -R * 1.05);
      ctx.lineTo(0, -R * 0.6);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(0, -R * 0.45, 3, 0, TAU);
      ctx.fillStyle = '#e05a5a';
      ctx.fill();
      return;
    }
    case 'cross':
      return; // drawn over the mouth
    case 'bar': {
      ctx.strokeStyle = '#6b6b76';
      ctx.setLineDash([6, 6]);
      ctx.beginPath();
      ctx.arc(0, 0, R, 0, TAU);
      ctx.stroke();
      ctx.setLineDash([]);
      return;
    }
  }
}

export { draw as __drawForTests };
