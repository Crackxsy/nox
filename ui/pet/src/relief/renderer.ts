/**
 * WebGL2 renderer for the relief creature.
 *
 * All per-vertex work runs in the vertex shader: linear-blend skinning against the bone matrices
 * (uniforms), the 3D head turn (blended by how much a vertex belongs to the head), the body turn,
 * the mild perspective and the relighting of the painted art by the rotated relief normal. The CPU
 * side per frame is the brain and skeleton (a few dozen bones) plus a handful of uniform uploads -
 * independent of mesh density. Static geometry is uploaded once.
 *
 * Alpha is premultiplied end to end: the pet window and the OBS source are transparent, and a
 * straight-alpha path would fringe every soft fur edge against whatever is behind it.
 *
 * Throws `ReliefRendererError` when WebGL2 is not there; the caller then falls back to the older
 * rig renderer (WebGL1 or Canvas 2D), which the pet keeps loaded for exactly that case.
 */

import type { Face } from './brain';
import { type Affine, applyX, applyY, rotYX } from './math';
import { OFFSETS, type ReliefGeometry, VERTEX_FLOATS } from './mesh';
import type { ReliefDefinition } from './reliefFile';
import type { Skeleton } from './skeleton';

export class ReliefRendererError extends Error {}

/** Room above and around the creature for bounces, ear springs and turns. */
const FRAME_SCALE = 0.92;
/** Where the ground line sits, as a fraction of the canvas height. */
const GROUND_V = 0.965;
const MAX_BONES = 32;

const VS = `#version 300 es
layout(location=0) in vec2 a_rest;
layout(location=1) in vec2 a_uv;
layout(location=2) in vec4 a_bones;
layout(location=3) in vec4 a_weights;
layout(location=4) in vec4 a_zn;
layout(location=5) in float a_head;
uniform vec4 u_bones[${MAX_BONES * 2}];
uniform mat3 u_R;
uniform mat3 u_B;
uniform vec3 u_hc;
uniform vec3 u_bc;
uniform vec3 u_light;
uniform float u_focal;
uniform vec4 u_place; // ground (art px), px per art px, squash
uniform vec4 u_view;  // canvas size (px), ground point (px)
out vec2 v_uv;
out float v_shade;
void main() {
  vec2 s = vec2(0.0);
  for (int k = 0; k < 4; k++) {
    int i = int(a_bones[k] + 0.5) * 2;
    vec4 m = u_bones[i];
    vec4 t = u_bones[i + 1];
    s += a_weights[k] * vec2(m.x * a_rest.x + m.z * a_rest.y + t.x, m.y * a_rest.x + m.w * a_rest.y + t.y);
  }
  vec3 p = vec3(s, a_zn.x);
  p = mix(p, u_R * (p - u_hc) + u_hc, a_head);
  p = u_B * (p - u_bc) + u_bc;
  float k = u_focal / (u_focal - p.z);
  vec2 d = (p.xy - u_place.xy) * u_place.z * k;
  d *= vec2(1.0 - 0.6 * u_place.w, 1.0 + u_place.w);
  vec2 c = (u_view.zw + d) / u_view.xy * 2.0 - 1.0;
  gl_Position = vec4(c.x, -c.y, 0.0, 1.0);
  v_uv = a_uv;
  vec3 n0 = a_zn.yzw;
  vec3 n1 = u_B * mix(n0, u_R * n0, a_head);
  // keep the painted light at rest; add only the change the rotation causes
  v_shade = clamp(1.0 + 0.6 * (dot(n1, u_light) - dot(n0, u_light)), 0.55, 1.35);
}`;

const PART_FS = `#version 300 es
precision highp float;
in vec2 v_uv;
in float v_shade;
uniform sampler2D u_tex;
uniform float u_bright;
out vec4 o;
void main() { vec4 c = texture(u_tex, v_uv); o = vec4(c.rgb * v_shade * u_bright, c.a); }`;

/**
 * Eye: the iris slides inside the ball ellipse (saccades); the upper lid is the fur above the eye
 * pulled down over the ball with a lash line on its edge; a lower lid rises for smiling eyes.
 */
const EYE_FS = `#version 300 es
precision highp float;
in vec2 v_uv;
in float v_shade;
uniform sampler2D u_tex;
uniform vec4 u_open, u_closed;
uniform vec2 u_c, u_r, u_iris;
uniform float u_lid, u_lower, u_bright;
out vec4 o;
vec2 at(vec4 r, vec2 l) { return r.xy + clamp(l, vec2(0.0), vec2(1.0)) * r.zw; }
void main(){
  vec2 p = (v_uv - u_c) / u_r;
  float d = length(p);
  float ins = 1.0 - smoothstep(1.02, 1.24, d);
  vec4 col = texture(u_tex, at(u_open, v_uv - u_iris * (1.0 - smoothstep(0.55, 0.95, d))));
  float xs = clamp(p.x / 1.25, -1.0, 1.0);
  float arc = sqrt(max(0.0, 1.0 - xs * xs));
  float yl = mix(-1.25, 0.95, u_lid) * arc;
  float yb = mix(1.25, 0.1, u_lower) * arc;
  float up = (1.0 - smoothstep(yl - 0.06, yl + 0.06, p.y)) * ins;
  float lo = smoothstep(yb - 0.06, yb + 0.06, p.y) * ins * step(0.001, u_lower);
  vec3 skin = texture(u_tex, at(u_closed, v_uv)).rgb;
  vec3 upper = skin * (0.94 - 0.2 * exp(-max(0.0, yl - p.y) / 0.14));
  vec3 lower = skin * (0.97 - 0.12 * exp(-max(0.0, p.y - yb) / 0.14));
  float sh = 1.0 - 0.4 * ins * step(yl, p.y) * exp(-(p.y - yl) / 0.22) * smoothstep(0.0, 0.25, u_lid);
  vec3 rgb = col.rgb * sh;
  rgb = mix(rgb, upper, up);
  rgb = mix(rgb, lower, lo * (1.0 - up));
  float lash = exp(-pow((p.y - yl) / 0.08, 2.0)) * (1.0 - smoothstep(0.95, 1.15, d)) * smoothstep(0.02, 0.2, u_lid) * 0.85;
  float lashB = exp(-pow((p.y - yb) / 0.07, 2.0)) * (1.0 - smoothstep(0.9, 1.1, d)) * smoothstep(0.02, 0.3, u_lower) * 0.5 * (1.0 - up);
  rgb = mix(rgb, vec3(0.11, 0.065, 0.045) * col.a, max(lash, lashB));
  o = vec4(rgb * v_shade * u_bright, col.a);
}`;

/** Screen-space quad (px) for the shadow, key poses and the transition composite. */
const QUAD_VS = `#version 300 es
layout(location=0) in vec2 a_corner;
uniform vec4 u_rect; // x0, y0, x1, y1 in px
uniform vec2 u_res;
out vec2 v_uv;
void main(){
  v_uv = a_corner;
  vec2 px = mix(u_rect.xy, u_rect.zw, a_corner);
  vec2 c = px / u_res * 2.0 - 1.0;
  gl_Position = vec4(c.x, -c.y, 0.0, 1.0);
}`;

const SHADOW_FS = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform float u_alpha;
out vec4 o;
void main(){
  vec2 p = v_uv * 2.0 - 1.0;
  float r = dot(p, p);
  o = vec4(0.0, 0.0, 0.0, u_alpha * exp(-r * 3.2) * (1.0 - smoothstep(0.7, 1.0, r)));
}`;

/** Key pose over the relief creature: `mix` 0 = relief only, 1 = pose only. */
const MIX_FS = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_pose;
uniform sampler2D u_relief;
uniform float u_mix, u_bright, u_useRelief;
uniform vec2 u_res;
out vec4 o;
void main(){
  vec4 p = texture(u_pose, v_uv) * u_bright;
  vec4 r = u_useRelief > 0.5 ? texture(u_relief, gl_FragCoord.xy / u_res) : vec4(0.0);
  o = mix(r, p, u_mix);
}`;

function compile(gl: WebGL2RenderingContext, vs: string, fs: string): WebGLProgram {
  const sh = (type: number, src: string): WebGLShader => {
    const s = gl.createShader(type);
    if (!s) throw new ReliefRendererError('out of shader objects');
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
      throw new ReliefRendererError(gl.getShaderInfoLog(s) ?? 'shader');
    return s;
  };
  const p = gl.createProgram();
  if (!p) throw new ReliefRendererError('out of program objects');
  gl.attachShader(p, sh(gl.VERTEX_SHADER, vs));
  gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS))
    throw new ReliefRendererError(gl.getProgramInfoLog(p) ?? 'link');
  return p;
}

type Uniforms = Record<string, WebGLUniformLocation | null>;
function uniforms(gl: WebGL2RenderingContext, p: WebGLProgram, names: string[]): Uniforms {
  return Object.fromEntries(names.map((n) => [n, gl.getUniformLocation(p, n)]));
}

const SKIN_UNIFORMS = [
  'u_bones',
  'u_R',
  'u_B',
  'u_hc',
  'u_bc',
  'u_light',
  'u_focal',
  'u_place',
  'u_view',
  'u_tex',
  'u_bright',
];

export interface FrameInput {
  face: Face;
  /** Whole-creature squash about the ground (pose switches), <0 squash. */
  squash: number;
  /** Key pose image and its opacity over the relief creature. */
  pose: TexImageSource | null;
  /** Scale of the key-pose art about the ground point (`relief.json` poses). */
  poseScale: number;
  poseAlpha: number;
  reliefVisible: boolean;
}

export class ReliefRenderer {
  readonly backend = 'webgl2' as const;
  private readonly gl: WebGL2RenderingContext;
  private readonly partProg: WebGLProgram;
  private readonly eyeProg: WebGLProgram;
  private readonly shadowProg: WebGLProgram;
  private readonly mixProg: WebGLProgram;
  private readonly uPart: Uniforms;
  private readonly uEye: Uniforms;
  private readonly uShadow: Uniforms;
  private readonly uMix: Uniforms;
  private readonly partVao: WebGLVertexArrayObject;
  private readonly eyeVao: WebGLVertexArrayObject;
  private readonly quadVao: WebGLVertexArrayObject;
  private readonly atlasTex: WebGLTexture;
  private readonly poseTex = new Map<TexImageSource, WebGLTexture>();
  private readonly buffers: WebGLBuffer[] = [];
  private readonly before: { first: number; count: number };
  private readonly after: { first: number; count: number };
  private readonly boneData = new Float32Array(MAX_BONES * 8);
  private readonly R = new Float32Array(9);
  private readonly B = new Float32Array(9);
  private readonly light: Float32Array;
  private fbo: WebGLFramebuffer | null = null;
  private fboTex: WebGLTexture | null = null;
  private fboSize = [0, 0];
  private w = 1;
  private h = 1;
  private scale = 1;
  private groundPx: [number, number] = [0, 0];
  private lost = false;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly def: ReliefDefinition,
    private readonly geo: ReliefGeometry,
    private readonly skel: Skeleton,
    atlas: TexImageSource,
  ) {
    if (skel.bones.length > MAX_BONES) throw new ReliefRendererError(`at most ${MAX_BONES} bones`);
    const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true, antialias: true });
    if (!gl) throw new ReliefRendererError('WebGL2 is not available');
    this.gl = gl;
    canvas.addEventListener('webglcontextlost', this.onLost);
    this.partProg = compile(gl, VS, PART_FS);
    this.eyeProg = compile(gl, VS, EYE_FS);
    this.shadowProg = compile(gl, QUAD_VS, SHADOW_FS);
    this.mixProg = compile(gl, QUAD_VS, MIX_FS);
    this.uPart = uniforms(gl, this.partProg, SKIN_UNIFORMS);
    this.uEye = uniforms(gl, this.eyeProg, [
      ...SKIN_UNIFORMS,
      'u_open',
      'u_closed',
      'u_c',
      'u_r',
      'u_iris',
      'u_lid',
      'u_lower',
    ]);
    this.uShadow = uniforms(gl, this.shadowProg, ['u_rect', 'u_res', 'u_alpha']);
    this.uMix = uniforms(gl, this.mixProg, [
      'u_rect',
      'u_res',
      'u_pose',
      'u_relief',
      'u_mix',
      'u_bright',
      'u_useRelief',
    ]);
    this.partVao = this.meshVao(geo.vertices, geo.indices);
    this.eyeVao = this.meshVao(geo.eyeVertices, geo.eyeIndices);
    this.quadVao = this.makeQuad();
    this.atlasTex = this.texture(atlas, true);
    const split = geo.ranges.findIndex((r) => r.name === '#eyes');
    const sum = (rs: typeof geo.ranges) => ({
      first: rs[0]?.first ?? 0,
      count: rs.reduce((s, r) => s + r.count, 0),
    });
    this.before = sum(split < 0 ? geo.ranges : geo.ranges.slice(0, split));
    this.after = sum(split < 0 ? [] : geo.ranges.slice(split + 1));
    const l = def.light;
    const n = Math.hypot(l[0], l[1], l[2]) || 1;
    this.light = new Float32Array([l[0] / n, l[1] / n, l[2] / n]);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  }

  private readonly onLost = (e: Event): void => {
    e.preventDefault();
    this.lost = true;
  };

  /** True once the GPU context is gone; the caller falls back. */
  get contextLost(): boolean {
    return this.lost || this.gl.isContextLost();
  }

  private buffer(): WebGLBuffer {
    const b = this.gl.createBuffer();
    if (!b) throw new ReliefRendererError('out of WebGL buffers');
    this.buffers.push(b);
    return b;
  }

  private meshVao(vertices: Float32Array, indices: Uint32Array): WebGLVertexArrayObject {
    const gl = this.gl;
    const vao = gl.createVertexArray();
    if (!vao) throw new ReliefRendererError('out of vertex arrays');
    gl.bindVertexArray(vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer());
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);
    const stride = VERTEX_FLOATS * 4;
    const attr = (loc: number, size: number, off: number) => {
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, off * 4);
    };
    attr(0, 2, OFFSETS.rest);
    attr(1, 2, OFFSETS.uv);
    attr(2, 4, OFFSETS.bones);
    attr(3, 4, OFFSETS.weights);
    attr(4, 4, OFFSETS.zn);
    attr(5, 1, OFFSETS.head);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.buffer());
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);
    gl.bindVertexArray(null);
    return vao;
  }

  private makeQuad(): WebGLVertexArrayObject {
    const gl = this.gl;
    const vao = gl.createVertexArray();
    if (!vao) throw new ReliefRendererError('out of vertex arrays');
    gl.bindVertexArray(vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    return vao;
  }

  private texture(img: TexImageSource, mip: boolean): WebGLTexture {
    const gl = this.gl;
    const t = gl.createTexture();
    if (!t) throw new ReliefRendererError('out of textures');
    gl.bindTexture(gl.TEXTURE_2D, t);
    // ImageBitmaps may arrive premultiplied already; images and canvases are premultiplied here.
    gl.pixelStorei(
      gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL,
      typeof ImageBitmap === 'undefined' || !(img instanceof ImageBitmap),
    );
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    if (mip) {
      gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    } else {
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    }
    return t;
  }

  private poseTexture(img: TexImageSource): WebGLTexture {
    let t = this.poseTex.get(img);
    if (!t) {
      t = this.texture(img, true);
      this.poseTex.set(img, t);
    }
    return t;
  }

  resize(cssSize: number, dpr: number): void {
    const px = Math.max(1, Math.round(cssSize * dpr));
    if (this.canvas.width !== px || this.canvas.height !== px) {
      this.canvas.width = px;
      this.canvas.height = px;
    }
    this.w = px;
    this.h = px;
    const art = this.def.artSize;
    this.scale = (px / art) * FRAME_SCALE;
    this.groundPx = [px / 2 + (this.def.ground[0] - art / 2) * this.scale, px * GROUND_V];
  }

  /** Art px -> canvas px for the flat layers (pose art, shadow), with the switch squash. */
  private artToPx(x: number, y: number, squash: number): [number, number] {
    const [gx, gy] = this.def.ground;
    return [
      this.groundPx[0] + (x - gx) * this.scale * (1 - 0.6 * squash),
      this.groundPx[1] + (y - gy) * this.scale * (1 + squash),
    ];
  }

  private ensureFbo(): void {
    const gl = this.gl;
    if (this.fbo && this.fboSize[0] === this.w && this.fboSize[1] === this.h) return;
    if (this.fbo) gl.deleteFramebuffer(this.fbo);
    if (this.fboTex) gl.deleteTexture(this.fboTex);
    this.fboTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.fboTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, this.w, this.h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    this.fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.fboTex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    this.fboSize = [this.w, this.h];
  }

  private skinUniforms(u: Uniforms, face: Face, squash: number): void {
    const gl = this.gl;
    gl.uniform4fv(u.u_bones, this.boneData);
    gl.uniformMatrix3fv(u.u_R, true, this.R);
    gl.uniformMatrix3fv(u.u_B, true, this.B);
    const { skel, def } = this;
    const hw = skel.world[skel.index.get('head')!];
    const bw = skel.world[skel.index.get('hips')!];
    gl.uniform3f(
      u.u_hc,
      applyX(hw, def.headCenter[0], def.headCenter[1]),
      applyY(hw, def.headCenter[0], def.headCenter[1]),
      def.headCenter[2],
    );
    gl.uniform3f(
      u.u_bc,
      applyX(bw, def.bodyCenter[0], def.bodyCenter[1]),
      applyY(bw, def.bodyCenter[0], def.bodyCenter[1]),
      def.bodyCenter[2],
    );
    gl.uniform3fv(u.u_light, this.light);
    gl.uniform1f(u.u_focal, def.focal);
    gl.uniform4f(u.u_place, def.ground[0], def.ground[1], this.scale, squash);
    gl.uniform4f(u.u_view, this.w, this.h, this.groundPx[0], this.groundPx[1]);
    gl.uniform1i(u.u_tex, 0);
    gl.uniform1f(u.u_bright, face.light);
  }

  private drawRelief(face: Face, squash: number): void {
    const gl = this.gl;
    const { def } = this;
    const W: Affine[] = this.skel.world;
    for (let i = 0; i < W.length; i++) {
      const m = W[i];
      const o = i * 8;
      this.boneData[o] = m[0];
      this.boneData[o + 1] = m[1];
      this.boneData[o + 2] = m[2];
      this.boneData[o + 3] = m[3];
      this.boneData[o + 4] = m[4];
      this.boneData[o + 5] = m[5];
    }
    rotYX(def.turn.yaw * face.yaw, -def.turn.pitch * face.pitch - 0.25 * face.sleep, this.R);
    rotYX(def.turn.body * face.bodyYaw, 0, this.B);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.atlasTex);

    gl.useProgram(this.partProg);
    this.skinUniforms(this.uPart, face, squash);
    gl.bindVertexArray(this.partVao);
    if (this.before.count)
      gl.drawElements(gl.TRIANGLES, this.before.count, gl.UNSIGNED_INT, this.before.first * 4);

    if (this.geo.eyes.length) {
      const u = this.uEye;
      gl.useProgram(this.eyeProg);
      this.skinUniforms(u, face, squash);
      gl.uniform1f(u.u_lid, face.lid);
      gl.uniform1f(u.u_lower, face.lower);
      gl.bindVertexArray(this.eyeVao);
      for (const e of this.geo.eyes) {
        gl.uniform4fv(u.u_open, e.openUv);
        gl.uniform4fv(u.u_closed, e.closedUv);
        gl.uniform2f(u.u_c, e.ellipse.c[0], e.ellipse.c[1]);
        gl.uniform2f(u.u_r, e.ellipse.r[0], e.ellipse.r[1]);
        gl.uniform2f(u.u_iris, face.iris[0] / e.size[0], face.iris[1] / e.size[1]);
        gl.drawElements(gl.TRIANGLES, e.count, gl.UNSIGNED_INT, e.first * 4);
      }
      gl.useProgram(this.partProg);
      gl.bindVertexArray(this.partVao);
    }
    if (this.after.count)
      gl.drawElements(gl.TRIANGLES, this.after.count, gl.UNSIGNED_INT, this.after.first * 4);
  }

  private drawShadow(face: Face, squash: number): void {
    const gl = this.gl;
    const rootW = this.skel.world[0];
    const [gx, gy] = this.def.ground;
    const k = Math.max(0.4, 1 - 1.8 * face.lift);
    const [cx] = this.artToPx(applyX(rootW, gx, gy), gy, 0);
    const sw = 150 * this.scale * k * (1 - 0.6 * squash);
    const sh = 20 * this.scale * k;
    const cy = this.groundPx[1] + 2 * this.scale;
    gl.useProgram(this.shadowProg);
    gl.uniform4f(this.uShadow.u_rect, cx - sw, cy - sh, cx + sw, cy + sh);
    gl.uniform2f(this.uShadow.u_res, this.w, this.h);
    gl.uniform1f(this.uShadow.u_alpha, 0.42 * Math.max(0.15, 1 - 2.6 * face.lift));
    gl.bindVertexArray(this.quadVao);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  frame(input: FrameInput): void {
    if (this.contextLost) return;
    const gl = this.gl;
    const { face, squash } = input;
    const mixing = input.pose !== null && input.poseAlpha > 0;
    const blend = mixing && input.reliefVisible;
    if (blend) {
      this.ensureFbo();
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
      gl.viewport(0, 0, this.w, this.h);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      this.drawRelief(face, squash);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }
    gl.viewport(0, 0, this.w, this.h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    this.drawShadow(face, squash);
    if (!mixing) {
      this.drawRelief(face, squash);
      return;
    }
    const art = this.def.artSize;
    const [gx, gy] = this.def.ground;
    const s = input.poseScale;
    const [x0, y0] = this.artToPx(gx - gx * s, gy - gy * s, squash);
    const [x1, y1] = this.artToPx(gx + (art - gx) * s, gy + (art - gy) * s, squash);
    const u = this.uMix;
    gl.useProgram(this.mixProg);
    gl.uniform4f(u.u_rect, x0, y0, x1, y1);
    gl.uniform2f(u.u_res, this.w, this.h);
    gl.uniform1f(u.u_mix, blend ? input.poseAlpha : 1);
    gl.uniform1f(u.u_bright, face.light);
    gl.uniform1f(u.u_useRelief, blend ? 1 : 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.poseTexture(input.pose!));
    gl.uniform1i(u.u_pose, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, blend ? this.fboTex : null);
    gl.uniform1i(u.u_relief, 1);
    gl.bindVertexArray(this.quadVao);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.activeTexture(gl.TEXTURE0);
  }

  dispose(): void {
    const gl = this.gl;
    this.canvas.removeEventListener('webglcontextlost', this.onLost);
    if (gl.isContextLost()) return;
    for (const b of this.buffers) gl.deleteBuffer(b);
    gl.deleteVertexArray(this.partVao);
    gl.deleteVertexArray(this.eyeVao);
    gl.deleteVertexArray(this.quadVao);
    gl.deleteTexture(this.atlasTex);
    for (const t of this.poseTex.values()) gl.deleteTexture(t);
    if (this.fbo) gl.deleteFramebuffer(this.fbo);
    if (this.fboTex) gl.deleteTexture(this.fboTex);
    for (const p of [this.partProg, this.eyeProg, this.shadowProg, this.mixProg]) gl.deleteProgram(p);
  }
}
