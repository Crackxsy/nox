"""Generate the placeholder sprite set for the `sprite:<id>` pet variant (#19).

The product owner has not picked the real creature yet and no art exists. This script draws a
deliberately simple, clean silhouette — a seated twilight night-creature in the same violet/amber
family the procedural variants use, so the placeholder drops into the window without looking like a
different product — and writes it out as one PNG with alpha per frame, plus the `sprites.json`
manifest the renderer validates.

Rendered at 512 px, not the 1024 px the real art will ship at, only to keep the committed
placeholder PNGs small (#19 asks for <= 40 KB each); the renderer scales whatever it is given.

Output (committed, so the sprite path is testable without running this):
    ui/pet/public/variants/placeholder/{idle,blink,listening-*,speaking-*,thinking-*,sleeping-*}.png
    ui/pet/public/variants/placeholder/sprites.json

Rendering goes through headless Chromium (Playwright) rather than Pillow: Pillow is not a project
dependency, Playwright already is, and Canvas 2D is the same drawing API `Pet.tsx` uses, so the
placeholder and the procedural creature stay visually related.

Run with the project venv:
    .venv/Scripts/python.exe ui/pet/scripts/make_placeholder_sprites.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT_DIR = Path(__file__).resolve().parents[1] / "public" / "variants" / "placeholder"
SIZE = 512

# One frame list per expression; the manifest below mirrors it.
FRAMES: dict[str, list[str]] = {
    "idle": ["idle.png"],
    "blink": ["blink.png"],
    "listening": ["listening-0.png", "listening-1.png"],
    "speaking": ["speaking-0.png", "speaking-1.png", "speaking-2.png"],
    "thinking": ["thinking-0.png", "thinking-1.png", "thinking-2.png"],
    "sleeping": ["sleeping-0.png", "sleeping-1.png"],
}

MANIFEST = {
    "id": "placeholder",
    "name": "Placeholder (generated silhouette)",
    "frames": FRAMES,
    "fps": 8,
    # The creature sits on its base: pin a point just above the feet to the window centre so the
    # idle breathing (transform-origin: bottom) does not make it hover.
    "anchor": [0.5, 0.56],
    "scale": 1.0,
}

# The drawing itself. `drawFrame(ctx, S, expression, frame)` renders one frame into a SxS canvas
# with a transparent background. Kept in one JS string so it runs in the page next to Canvas 2D.
DRAW_JS = r"""
(args) => {
  const { size, expression, frame } = args;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  const S = size;
  const TAU = Math.PI * 2;

  // Palette: the twilight violet of the procedural variants, one warm amber rim as the only
  // accent, near-black eyes with a single pale glint. Carries its own contrast, because the pet
  // window is transparent and the desktop behind it is unknown.
  const BODY = '#4e3aa8';
  const BODY_HI = '#7a63d8';
  const BELLY = '#8f7ce4';
  const RIM = '#e2a04a';
  const INK = '#17141f';
  const GLINT = '#f2f0fa';

  const cx = S * 0.5;
  const groundY = S * 0.90;

  const sleeping = expression === 'sleeping';
  const listening = expression === 'listening';
  const thinking = expression === 'thinking';
  const speaking = expression === 'speaking';
  const blink = expression === 'blink';

  // Per-frame motion: a slow settle for sleeping, one ear flick for listening, an orbiting dot for
  // thinking, a mouth cycle for speaking. Everything else is identical between frames on purpose —
  // the idle breathing is a CSS transform in the renderer, not baked into the art.
  const settle = sleeping ? frame * 0.012 : 0;
  const earFlick = listening && frame === 1 ? -0.12 : 0;
  const bodyH = S * (sleeping ? 0.30 : 0.33) * (1 - settle);
  const bodyW = S * (sleeping ? 0.26 : 0.235) * (1 + settle * 0.6);
  const bodyCy = groundY - bodyH;

  ctx.clearRect(0, 0, S, S);
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';

  // --- tail: a thick plume sweeping up the right side, behind the body -------------------------
  ctx.save();
  ctx.translate(cx + bodyW * 0.60, groundY - bodyH * 0.18);
  ctx.rotate(0.10);
  ctx.strokeStyle = BODY;
  ctx.lineWidth = bodyW * 0.42;
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.bezierCurveTo(
    bodyW * 1.35, bodyH * 0.06,
    bodyW * 1.55, -bodyH * 0.52,
    bodyW * 0.80, -bodyH * 0.80,
  );
  ctx.stroke();
  ctx.fillStyle = BODY_HI;
  ctx.beginPath();
  ctx.ellipse(bodyW * 0.80, -bodyH * 0.80, bodyW * 0.27, bodyW * 0.24, -0.4, 0, TAU);
  ctx.fill();
  ctx.restore();

  // --- ears -------------------------------------------------------------------------------------
  const earBaseY = bodyCy - bodyH * 0.58;
  for (const s of [-1, 1]) {
    const lean = sleeping ? 0.5 * s : (listening ? -0.06 * s : 0.06 * s) + (s > 0 ? earFlick : 0);
    ctx.save();
    ctx.translate(cx + s * bodyW * 0.52, earBaseY);
    ctx.rotate(lean);
    const eh = bodyH * (sleeping ? 0.42 : 0.62);
    const ew = bodyW * 0.36;
    ctx.fillStyle = BODY;
    ctx.beginPath();
    ctx.moveTo(-ew, eh * 0.30);
    ctx.quadraticCurveTo(-ew * 0.55, -eh, s * ew * 0.35, -eh * 1.02);
    ctx.quadraticCurveTo(ew * 0.75, -eh * 0.2, ew, eh * 0.34);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = BELLY;
    ctx.beginPath();
    ctx.moveTo(-ew * 0.45, eh * 0.22);
    ctx.quadraticCurveTo(-ew * 0.2, -eh * 0.55, s * ew * 0.18, -eh * 0.62);
    ctx.quadraticCurveTo(ew * 0.42, -eh * 0.1, ew * 0.5, eh * 0.24);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  // --- body -------------------------------------------------------------------------------------
  const bodyPath = () => {
    ctx.beginPath();
    ctx.moveTo(cx - bodyW * 0.62, groundY);
    ctx.bezierCurveTo(
      cx - bodyW * 1.06, groundY - bodyH * 0.55,
      cx - bodyW * 0.92, bodyCy - bodyH * 0.62,
      cx, bodyCy - bodyH * 0.66,
    );
    ctx.bezierCurveTo(
      cx + bodyW * 0.92, bodyCy - bodyH * 0.62,
      cx + bodyW * 1.06, groundY - bodyH * 0.55,
      cx + bodyW * 0.62, groundY,
    );
    ctx.closePath();
  };
  ctx.fillStyle = BODY;
  bodyPath();
  ctx.fill();

  // Rim light: a warm edge on the lower-left flank only, clipped to the body, as if one low lamp
  // stood to the creature's left. The single accent in the whole set; it stops at the shoulder
  // rather than tracing the whole outline, which would read as a sticker border.
  ctx.save();
  bodyPath();
  ctx.clip();
  ctx.strokeStyle = RIM;
  ctx.lineWidth = bodyW * 0.07;
  ctx.beginPath();
  ctx.moveTo(cx - bodyW * 0.60, groundY - bodyH * 0.02);
  ctx.quadraticCurveTo(
    cx - bodyW * 0.99, groundY - bodyH * 0.52,
    cx - bodyW * 0.91, bodyCy - bodyH * 0.16,
  );
  ctx.stroke();
  ctx.restore();

  // chest, a lighter panel that keeps the silhouette from reading as one flat blob
  ctx.fillStyle = BELLY;
  ctx.beginPath();
  ctx.ellipse(cx, groundY - bodyH * 0.42, bodyW * 0.42, bodyH * 0.44, 0, 0, TAU);
  ctx.fill();

  // forepaws
  ctx.fillStyle = BODY_HI;
  for (const s of [-1, 1]) {
    ctx.beginPath();
    ctx.ellipse(
      cx + s * bodyW * 0.34, groundY - bodyH * 0.055,
      bodyW * 0.22, bodyH * 0.085, 0, 0, TAU,
    );
    ctx.fill();
  }

  // --- face -------------------------------------------------------------------------------------
  const faceY = bodyCy - bodyH * 0.18;
  const eyeDx = bodyW * 0.36;
  const eyeR = bodyW * 0.155;
  const shut = sleeping || blink;

  for (const s of [-1, 1]) {
    const ex = cx + s * eyeDx;
    if (shut) {
      ctx.strokeStyle = INK;
      ctx.lineWidth = S * 0.011;
      ctx.beginPath();
      ctx.moveTo(ex - eyeR, faceY);
      ctx.quadraticCurveTo(ex, faceY + eyeR * 0.75, ex + eyeR, faceY);
      ctx.stroke();
      continue;
    }
    const openK = listening ? 1.12 : thinking ? 0.92 : 1;
    ctx.fillStyle = INK;
    ctx.beginPath();
    ctx.ellipse(ex, faceY, eyeR * 0.82, eyeR * openK, 0, 0, TAU);
    ctx.fill();
    ctx.fillStyle = GLINT;
    const lookUp = thinking ? -eyeR * 0.30 : 0;
    ctx.beginPath();
    ctx.arc(ex + eyeR * 0.26, faceY - eyeR * 0.30 + lookUp, eyeR * 0.28, 0, TAU);
    ctx.fill();
  }

  // muzzle + mouth
  const mouthY = faceY + bodyH * 0.30;
  ctx.fillStyle = INK;
  ctx.beginPath();
  ctx.ellipse(cx, mouthY - bodyH * 0.10, bodyW * 0.075, bodyW * 0.055, 0, 0, TAU);
  ctx.fill();

  if (speaking) {
    const openness = [0.30, 0.95, 0.60][frame] ?? 0.5;
    ctx.fillStyle = INK;
    ctx.beginPath();
    ctx.ellipse(cx, mouthY, bodyW * 0.15, bodyH * 0.105 * openness, 0, 0, TAU);
    ctx.fill();
  } else if (!shut) {
    ctx.strokeStyle = INK;
    ctx.lineWidth = S * 0.010;
    ctx.beginPath();
    ctx.moveTo(cx - bodyW * 0.14, mouthY - bodyH * 0.02);
    ctx.quadraticCurveTo(cx, mouthY + bodyH * 0.045, cx + bodyW * 0.14, mouthY - bodyH * 0.02);
    ctx.stroke();
  }

  // --- thinking: three dots rising beside the head, lighting up in turn --------------------------
  // Not an orbit: three dots 120 deg apart rotating by 120 deg per frame renders three *identical*
  // PNGs, and an orbit is unreadable in three frames anyway. A sequential fill reads as "working
  // on it" at any frame rate.
  if (thinking) {
    for (let i = 0; i < 3; i++) {
      ctx.globalAlpha = i === frame ? 1 : 0.26;
      ctx.fillStyle = BELLY;
      ctx.beginPath();
      ctx.arc(
        cx + bodyW * (0.98 + i * 0.34),
        earBaseY - bodyH * (0.10 + i * 0.24),
        S * (0.015 + i * 0.005),
        0,
        TAU,
      );
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  // --- sleeping: one small breath mark ----------------------------------------------------------
  if (sleeping) {
    ctx.globalAlpha = frame === 0 ? 0.85 : 0.45;
    ctx.fillStyle = BELLY;
    ctx.beginPath();
    ctx.arc(cx + bodyW * 0.95, earBaseY - bodyH * 0.10, S * 0.016 + frame * S * 0.008, 0, TAU);
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  return canvas.toDataURL('image/png');
}
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 64, "height": 64})
        page.set_content("<!doctype html><html><body></body></html>")
        for expression, files in FRAMES.items():
            for index, file_name in enumerate(files):
                data_url: str = page.evaluate(
                    DRAW_JS, {"size": SIZE, "expression": expression, "frame": index}
                )
                payload = base64.b64decode(data_url.split(",", 1)[1])
                path = OUT_DIR / file_name
                path.write_bytes(payload)
                sys.stdout.write(f"{path.name}: {len(payload) / 1024:.1f} KB\n")
        browser.close()

    (OUT_DIR / "sprites.json").write_text(
        json.dumps(MANIFEST, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    sys.stdout.write(f"wrote {OUT_DIR / 'sprites.json'}\n")


if __name__ == "__main__":
    main()
