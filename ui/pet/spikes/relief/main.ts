/**
 * Prototype C, "Relief 2.5D": the same painted parts and bones as Rig 2.0, but every part is a
 * height field. The height is derived automatically from the part's silhouette (inflation) plus a
 * few anatomical bumps, so head turns and body turns become real 3D rotations with parallax and
 * a matching relighting delta - the "angle X/Y" deformers a Live2D artist would otherwise build
 * by hand. Flat WebGL2, no dependencies.
 */
import { ART_HEIGHT, BONES, Brain, GROUND, WEIGHTS } from '../shared/eulenfuchs';
import { DynMesh, EYE_FS, SHADOW_FS, program, texture } from '../shared/gl';
import { type Spike, type SpikeContext, boot } from '../shared/harness';
import { EYE_SHAPE, type LoadedPart, loadParts } from '../shared/parts';
import { type PartMesh, Skeleton, apply, buildPartMesh, skin } from '../shared/rig';

const VS = `#version 300 es
layout(location=0) in vec2 a_pos;
layout(location=1) in vec2 a_uv;
layout(location=2) in float a_shade;
uniform vec2 u_res;
out vec2 v_uv;
out float v_shade;
void main(){ v_uv = a_uv; v_shade = a_shade; vec2 c = a_pos / u_res * 2.0 - 1.0; gl_Position = vec4(c.x, -c.y, 0.0, 1.0); }`;

const PART_FS = `#version 300 es
precision highp float;
in vec2 v_uv;
in float v_shade;
uniform sampler2D u_tex;
out vec4 o;
void main(){ vec4 c = texture(u_tex, v_uv); o = vec4(c.rgb * v_shade, c.a); }`;

const EYE_VS = `#version 300 es
layout(location=0) in vec2 a_pos;
layout(location=1) in vec2 a_uv;
uniform vec2 u_res;
out vec2 v_uv;
void main(){ v_uv = a_uv; vec2 c = a_pos / u_res * 2.0 - 1.0; gl_Position = vec4(c.x, -c.y, 0.0, 1.0); }`;

const ORDER = ['tail_l', 'body', 'tail_r', 'wing_l', 'wing_r', 'head', '#eyes', 'ear_l', 'ear_r', 'jaw'];
const CELL: Record<string, number> = { body: 5, head: 4, jaw: 3, ear_l: 4, ear_r: 4 };
/** Depth of each part's base plane (art px, towards the viewer). */
const LAYER_Z: Record<string, number> = {
  tail_l: -30, body: 0, tail_r: 18, wing_l: 14, wing_r: 14, head: 22, ear_l: 4, ear_r: 4, jaw: 30,
};
const HEAD_BONES = new Set(['head', 'jaw', 'cheek_l', 'cheek_r', 'ear_l', 'ear_l2', 'ear_r', 'ear_r2']);
const HEAD_CENTER = [260, 252, 20] as const;
const BODY_CENTER = [260, 420, 0] as const;
const LIGHT = (() => {
  const v = [-0.45, -0.65, 0.75];
  const l = Math.hypot(v[0], v[1], v[2]);
  return v.map((x) => x / l);
})();
const FOCAL = 1500;

interface Drawn {
  name: string;
  mesh?: PartMesh;
  gpu: DynMesh;
  tex: WebGLTexture;
  z?: Float32Array;
  normal?: Float32Array;
  headness?: Float32Array;
  shade?: Float32Array;
  screen?: Float32Array;
  eye?: { rect: [number, number, number, number]; z: number; closed: WebGLTexture; shape: { c: [number, number]; r: [number, number] } };
}

type M3 = number[]; // row-major 3x3

const rotYX = (yaw: number, pitch: number): M3 => {
  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);
  // R = Ry(yaw) * Rx(pitch); y is down, z towards viewer; positive yaw turns the face to screen-right
  return [cy, sy * sp, sy * cp, 0, cp, -sp, -sy, cy * sp, cy * cp];
};
const mv = (m: M3, x: number, y: number, z: number): [number, number, number] => [
  m[0] * x + m[1] * y + m[2] * z,
  m[3] * x + m[4] * y + m[5] * z,
  m[6] * x + m[7] * y + m[8] * z,
];

function sampleDepth(p: LoadedPart, x: number, y: number): number {
  if (!p.depthData) return 0;
  const [rx, ry, rw, rh] = p.rect;
  const ix = Math.max(0, Math.min(rw - 1, Math.round(x - rx)));
  const iy = Math.max(0, Math.min(rh - 1, Math.round(y - ry)));
  return p.depthData[(iy * rw + ix) * 4] / 255;
}

class Relief implements Spike {
  title = 'C · Relief 2.5D';
  subtitle = 'gemalte Teile + automatische Tiefe: echte Kopf-/Körperdrehung, Licht folgt';
  gl!: WebGL2RenderingContext;
  private ctx!: SpikeContext;
  private skel = new Skeleton(BONES);
  private brain = new Brain();
  private drawn: Drawn[] = [];
  private partProg!: WebGLProgram;
  private eyeProg!: WebGLProgram;
  private shadowProg!: WebGLProgram;
  private shadow!: DynMesh;
  private scale = 1;

  async init(canvas: HTMLCanvasElement, ctx: SpikeContext): Promise<void> {
    this.ctx = ctx;
    const record = new URLSearchParams(location.search).get('mode') === 'record';
    const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true, antialias: true, preserveDrawingBuffer: record });
    if (!gl) throw new Error('WebGL2 not available');
    this.gl = gl;
    this.partProg = program(gl, VS, PART_FS);
    this.eyeProg = program(gl, EYE_VS, EYE_FS);
    this.shadowProg = program(gl, EYE_VS, SHADOW_FS);
    this.scale = ctx.H / ART_HEIGHT;
    // the skeleton works in art px; projection to the screen happens after the 3D step
    this.skel.placement = new Float64Array([1, 0, 0, 1, 0, 0]);

    const set = await loadParts('/eulenfuchs', true);
    const head = set.parts.find((p) => p.name === 'head')!;
    for (const name of ORDER) {
      if (name === '#eyes') {
        for (const e of set.eyes) {
          // the eye is a decal: a small grid that follows the head's relief, so the opaque patch
          // stays glued to the face when the head turns
          const mesh = buildPartMesh(e.name, e.rect, null, 8, this.skel, () => [['head', 1]]);
          const n = mesh.rest.length / 2;
          const z = new Float32Array(n);
          for (let v = 0; v < n; v++)
            z[v] = LAYER_Z.head + (head.relief ?? 0) * sampleDepth(head, mesh.rest[v * 2], mesh.rest[v * 2 + 1]) + 0.5;
          const screen = new Float32Array(n * 2);
          const gpu = new DynMesh(gl, screen, 2, mesh.uv, mesh.indices);
          this.drawn.push({ name: e.name, mesh, gpu, z, screen, tex: texture(gl, e.image), eye: { rect: e.rect, z: 0, closed: texture(gl, e.closedImage!), shape: EYE_SHAPE[e.name] } });
        }
        continue;
      }
      const p = set.parts.find((q) => q.name === name)!;
      const mesh = buildPartMesh(name, p.rect, p.alpha, CELL[name] ?? 4, this.skel, WEIGHTS[name]);
      const n = mesh.rest.length / 2;
      const z = new Float32Array(n);
      const normal = new Float32Array(n * 3);
      const headness = new Float32Array(n);
      const relief = p.relief ?? 0;
      for (let v = 0; v < n; v++) {
        const x = mesh.rest[v * 2];
        const y = mesh.rest[v * 2 + 1];
        z[v] = name === 'jaw'
          ? LAYER_Z.head + (head.relief ?? 0) * sampleDepth(head, x, y) + 1 // glued to the muzzle surface
          : (LAYER_Z[name] ?? 0) + relief * sampleDepth(p, x, y);
        const dx = (relief * (sampleDepth(p, x + 3, y) - sampleDepth(p, x - 3, y))) / 6;
        const dy = (relief * (sampleDepth(p, x, y + 3) - sampleDepth(p, x, y - 3))) / 6;
        const l = Math.hypot(dx, dy, 1);
        normal.set([-dx / l, -dy / l, 1 / l], v * 3);
        let hw = 0;
        for (let k = 0; k < 4; k++) {
          const b = BONES[mesh.bones[v * 4 + k]].name;
          const w = mesh.weights[v * 4 + k];
          if (HEAD_BONES.has(b)) hw += w;
          else if (b === 'neck') hw += 0.45 * w;
        }
        headness[v] = hw;
      }
      const shade = new Float32Array(n).fill(1);
      const screen = new Float32Array(n * 2);
      const gpu = new DynMesh(gl, screen, 2, mesh.uv, mesh.indices, { data: shade, size: 1, loc: 2 });
      this.drawn.push({ name, mesh, gpu, tex: texture(gl, p.image), z, normal, headness, shade, screen });
    }
    this.shadow = new DynMesh(gl, new Float32Array(8), 2, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), new Uint16Array([0, 1, 2, 1, 3, 2]));
  }

  reset(): void {
    this.brain.reset();
    this.skel.resetPhysics();
  }

  /** art px (after 2D skinning) + depth -> screen px with mild perspective */
  private project(x: number, y: number, z: number): [number, number] {
    const k = FOCAL / (FOCAL - z);
    return [this.ctx.centerX + (x - GROUND[0]) * this.scale * k, this.ctx.groundY + (y - GROUND[1]) * this.scale * k];
  }

  frame(t: number, dt: number): void {
    const { gl, ctx, skel } = this;
    const face = this.brain.update(t, dt, skel, ctx.envelope, ctx);
    const R = rotYX(0.42 * face.yaw, -0.28 * face.pitch - 0.25 * face.sleep);
    const B = rotYX(0.32 * face.bodyYaw, 0);
    const rootW = skel.world[skel.index.get('root')!];
    const hipW = skel.world[skel.index.get('hips')!];
    const headW = skel.world[skel.index.get('head')!];
    const hc = apply(headW, HEAD_CENTER[0], HEAD_CENTER[1]);
    const bc = apply(hipW, BODY_CENTER[0], BODY_CENTER[1]);

    const xf = (x: number, y: number, z: number, h: number): [number, number, number] => {
      // head rotation (blended by how much the vertex belongs to the head), then body turn
      let p: [number, number, number] = [x, y, z];
      if (h > 0) {
        const r = mv(R, x - hc[0], y - hc[1], z - HEAD_CENTER[2]);
        const q = [r[0] + hc[0], r[1] + hc[1], r[2] + HEAD_CENTER[2]];
        p = [x + (q[0] - x) * h, y + (q[1] - y) * h, z + (q[2] - z) * h];
      }
      const r = mv(B, p[0] - bc[0], p[1] - bc[1], p[2] - BODY_CENTER[2]);
      return [r[0] + bc[0], r[1] + bc[1], r[2] + BODY_CENTER[2]];
    };

    gl.viewport(0, 0, ctx.w, ctx.h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

    const g = this.project(apply(rootW, GROUND[0], GROUND[1])[0], GROUND[1], 0);
    const sw = 150 * this.scale * Math.max(0.4, face.shadowScale);
    const shh = 20 * this.scale * Math.max(0.4, face.shadowScale);
    const gy = ctx.groundY + 2 * this.scale;
    this.shadow.update(new Float32Array([g[0] - sw, gy - shh, g[0] + sw, gy - shh, g[0] - sw, gy + shh, g[0] + sw, gy + shh]));
    gl.useProgram(this.shadowProg);
    gl.uniform2f(gl.getUniformLocation(this.shadowProg, 'u_res'), ctx.w, ctx.h);
    gl.uniform1f(gl.getUniformLocation(this.shadowProg, 'u_alpha'), 0.42 * Math.max(0.15, face.shadowAlpha));
    this.shadow.draw();

    const nl = (n: [number, number, number]): number => n[0] * LIGHT[0] + n[1] * LIGHT[1] + n[2] * LIGHT[2];
    for (const d of this.drawn) {
      if (d.eye) {
        const [, , w, h] = d.eye.rect;
        const mesh = d.mesh!;
        skin(mesh, skel);
        for (let v = 0, n = mesh.rest.length / 2; v < n; v++) {
          const p = xf(mesh.skinned[v * 2], mesh.skinned[v * 2 + 1], d.z![v], 1);
          const s = this.project(p[0], p[1], p[2]);
          d.screen![v * 2] = s[0];
          d.screen![v * 2 + 1] = s[1];
        }
        d.gpu.update(d.screen!);
        gl.useProgram(this.eyeProg);
        const u = (n: string) => gl.getUniformLocation(this.eyeProg, n);
        gl.uniform2f(u('u_res'), ctx.w, ctx.h);
        gl.uniform2f(u('u_c'), ...d.eye.shape.c);
        gl.uniform2f(u('u_r'), ...d.eye.shape.r);
        gl.uniform2f(u('u_iris'), face.iris[0] / w, face.iris[1] / h);
        gl.uniform1f(u('u_lid'), d.name === 'eye_l' ? face.lidL : face.lidR);
        gl.uniform1f(u('u_lower'), face.lower);
        // the eye ball catches less light when the head turns away from the light
        const n = mv(R, 0, 0, 1);
        gl.uniform1f(u('u_light'), 1 + 0.5 * (nl(n) - LIGHT[2]));
        gl.uniform1i(u('u_tex'), 0);
        gl.uniform1i(u('u_closed'), 1);
        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, d.eye.closed);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, d.tex);
        d.gpu.draw();
        continue;
      }
      const mesh = d.mesh!;
      skin(mesh, skel);
      const n = mesh.rest.length / 2;
      for (let v = 0; v < n; v++) {
        const h = d.headness![v];
        const p = xf(mesh.skinned[v * 2], mesh.skinned[v * 2 + 1], d.z![v], h);
        const s = this.project(p[0], p[1], p[2]);
        d.screen![v * 2] = s[0];
        d.screen![v * 2 + 1] = s[1];
        const n0: [number, number, number] = [d.normal![v * 3], d.normal![v * 3 + 1], d.normal![v * 3 + 2]];
        let n1 = h > 0 ? mv(R, ...n0) : n0;
        if (h > 0 && h < 1) n1 = [n0[0] + (n1[0] - n0[0]) * h, n0[1] + (n1[1] - n0[1]) * h, n0[2] + (n1[2] - n0[2]) * h];
        n1 = mv(B, ...n1);
        // relight: keep the painted light at rest, add only the change caused by the rotation
        d.shade![v] = Math.max(0.55, Math.min(1.35, 1 + 0.6 * (nl(n1) - nl(n0))));
      }
      d.gpu.update(d.screen!, d.shade);
      gl.useProgram(this.partProg);
      gl.uniform2f(gl.getUniformLocation(this.partProg, 'u_res'), ctx.w, ctx.h);
      gl.bindTexture(gl.TEXTURE_2D, d.tex);
      d.gpu.draw();
    }
  }
}

void boot(new Relief());
