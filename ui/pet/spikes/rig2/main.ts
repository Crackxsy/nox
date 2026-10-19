/**
 * Prototype A, "Rig 2.0": the existing painted Eulenfuchs, cut into overlapping parts, driven by a
 * 2D bone hierarchy with linear-blend mesh skinning, jiggle-bone secondary motion, squash &
 * stretch, procedural hops and saccadic eyes. Flat WebGL2, no dependencies.
 */
import { ART_HEIGHT, BONES, Brain, GROUND, WEIGHTS } from '../shared/eulenfuchs';
import { DynMesh, EYE_FS, SHADOW_FS, program, texture } from '../shared/gl';
import { type Spike, type SpikeContext, boot } from '../shared/harness';
import { EYE_SHAPE, loadParts } from '../shared/parts';
import { type Mat, type PartMesh, Skeleton, apply, buildPartMesh, mul, skin } from '../shared/rig';

const VS = `#version 300 es
layout(location=0) in vec2 a_pos;
layout(location=1) in vec2 a_uv;
uniform vec2 u_res;
out vec2 v_uv;
void main(){ v_uv = a_uv; vec2 c = a_pos / u_res * 2.0 - 1.0; gl_Position = vec4(c.x, -c.y, 0.0, 1.0); }`;

const PART_FS = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_tex;
out vec4 o;
void main(){ o = texture(u_tex, v_uv); }`;

const ORDER = ['tail_l', 'body', 'tail_r', 'wing_l', 'wing_r', 'head', '#eyes', 'ear_l', 'ear_r', 'jaw'];
const CELL: Record<string, number> = { body: 5, head: 5, jaw: 3, ear_l: 4, ear_r: 4 };

interface Drawn {
  name: string;
  mesh?: PartMesh;
  gpu: DynMesh;
  tex: WebGLTexture;
  eye?: { rect: [number, number, number, number]; shape: { c: [number, number]; r: [number, number] }; closed: WebGLTexture };
}

class Rig2 implements Spike {
  title = 'A · Rig 2.0';
  subtitle = '2D-Skelett + Mesh-Skinning + Feder-Physik · bestehende Zeichnung';
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
    this.eyeProg = program(gl, VS, EYE_FS);
    this.shadowProg = program(gl, VS, SHADOW_FS);

    this.scale = ctx.H / ART_HEIGHT;
    const s = this.scale;
    this.skel.placement = new Float64Array([s, 0, 0, s, ctx.centerX - GROUND[0] * s, ctx.groundY - GROUND[1] * s]);

    const set = await loadParts('/eulenfuchs');
    for (const name of ORDER) {
      if (name === '#eyes') {
        for (const e of set.eyes) {
          const quad = new Float32Array(8);
          const gpu = new DynMesh(gl, quad, 2, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), new Uint16Array([0, 1, 2, 1, 3, 2]));
          this.drawn.push({ name: e.name, gpu, tex: texture(gl, e.image), eye: { rect: e.rect, shape: EYE_SHAPE[e.name], closed: texture(gl, e.closedImage!) } });
        }
        continue;
      }
      const p = set.parts.find((q) => q.name === name)!;
      const mesh = buildPartMesh(name, p.rect, p.alpha, CELL[name] ?? 4, this.skel, WEIGHTS[name]);
      const gpu = new DynMesh(gl, mesh.skinned, 2, mesh.uv, mesh.indices);
      this.drawn.push({ name, mesh, gpu, tex: texture(gl, p.image) });
    }
    this.shadow = new DynMesh(gl, new Float32Array(8), 2, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), new Uint16Array([0, 1, 2, 1, 3, 2]));
  }

  reset(): void {
    this.brain.reset();
    this.skel.resetPhysics();
  }

  frame(t: number, dt: number): void {
    const { gl, ctx, skel } = this;
    const face = this.brain.update(t, dt, skel, ctx.envelope, ctx);

    gl.viewport(0, 0, ctx.w, ctx.h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

    // contact shadow on the ground under the root
    const rootW = skel.world[skel.index.get('root')!];
    const gx = apply(rootW, GROUND[0], GROUND[1])[0];
    const sw = 150 * this.scale * Math.max(0.4, face.shadowScale);
    const shh = 20 * this.scale * Math.max(0.4, face.shadowScale);
    const gy = ctx.groundY + 2 * this.scale;
    this.shadow.update(new Float32Array([gx - sw, gy - shh, gx + sw, gy - shh, gx - sw, gy + shh, gx + sw, gy + shh]));
    gl.useProgram(this.shadowProg);
    gl.uniform2f(gl.getUniformLocation(this.shadowProg, 'u_res'), ctx.w, ctx.h);
    gl.uniform1f(gl.getUniformLocation(this.shadowProg, 'u_alpha'), 0.42 * Math.max(0.15, face.shadowAlpha));
    this.shadow.draw();

    const headW = skel.world[skel.index.get('head')!];
    for (const d of this.drawn) {
      if (d.eye) {
        // eyes ride on the head, shifted a little further than the head for a parallax hint
        const [x, y, w, h] = d.eye.rect;
        const px = 3.5 * face.yaw;
        const py = 2.0 * face.pitch;
        const m: Mat = mul(headW, new Float64Array([1, 0, 0, 1, px, py]));
        const q = [apply(m, x, y), apply(m, x + w, y), apply(m, x, y + h), apply(m, x + w, y + h)].flat();
        d.gpu.update(new Float32Array(q));
        gl.useProgram(this.eyeProg);
        const u = (n: string) => gl.getUniformLocation(this.eyeProg, n);
        gl.uniform2f(u('u_res'), ctx.w, ctx.h);
        gl.uniform2f(u('u_c'), ...d.eye.shape.c);
        gl.uniform2f(u('u_r'), ...d.eye.shape.r);
        gl.uniform2f(u('u_iris'), face.iris[0] / w, face.iris[1] / h);
        gl.uniform1f(u('u_lid'), d.name === 'eye_l' ? face.lidL : face.lidR);
        gl.uniform1f(u('u_lower'), face.lower);
        gl.uniform1f(u('u_light'), 1);
        gl.uniform1i(u('u_tex'), 0);
        gl.uniform1i(u('u_closed'), 1);
        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, d.eye.closed);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, d.tex);
        d.gpu.draw();
        continue;
      }
      skin(d.mesh!, skel);
      d.gpu.update(d.mesh!.skinned);
      gl.useProgram(this.partProg);
      gl.uniform2f(gl.getUniformLocation(this.partProg, 'u_res'), ctx.w, ctx.h);
      gl.bindTexture(gl.TEXTURE_2D, d.tex);
      d.gpu.draw();
    }
  }
}

void boot(new Rig2());
