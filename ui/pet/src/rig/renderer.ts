/**
 * Drawing a skinned mesh, on WebGL where it exists and on Canvas 2D where it does not.
 *
 * Both backends take the same draw calls — deformed positions in normalised image coordinates,
 * texture coordinates, triangle indices and an alpha — so nothing above this file knows or cares
 * which one is running. What differs is honesty about cost: WebGL draws the whole grid in one
 * call, while the 2D fallback has to clip and transform every triangle by hand, so it advertises a
 * much smaller `triangleBudget` and the caller coarsens the mesh to match. The downgrade is
 * reported, never hidden: `backend` and `downgradeReason` are read by the pet and surfaced.
 *
 * Alpha is premultiplied end to end. The pet window is transparent, so a straight-alpha path would
 * fringe every soft edge of the creature against whatever is behind it.
 */

export interface DrawCall {
  image: TexImageSource;
  /** Deformed vertex positions in normalised image coordinates. */
  positions: Float32Array;
  uv: Float32Array;
  indices: Uint16Array;
  /** 0..1. Used for cross-dissolving between key poses. */
  alpha: number;
}

export interface RigRenderer {
  readonly backend: 'webgl' | 'canvas2d';
  /** Why WebGL is not in use, in words a log line can carry. `null` when it is. */
  readonly downgradeReason: string | null;
  /** Triangles this backend can draw per frame without missing 60 fps. */
  readonly triangleBudget: number;
  resize(cssSize: number, devicePixelRatio: number): void;
  draw(calls: readonly DrawCall[]): void;
  dispose(): void;
}

const VERTEX_SHADER = `
attribute vec2 a_position;
attribute vec2 a_uv;
varying vec2 v_uv;
void main() {
  v_uv = a_uv;
  gl_Position = vec4(a_position.x * 2.0 - 1.0, 1.0 - a_position.y * 2.0, 0.0, 1.0);
}`;

const FRAGMENT_SHADER = `
precision mediump float;
uniform sampler2D u_texture;
uniform float u_alpha;
varying vec2 v_uv;
void main() {
  gl_FragColor = texture2D(u_texture, v_uv) * u_alpha;
}`;

/** A 20x20 base grid is 800 triangles; WebGL does not notice, so the budget only has to not bite. */
const WEBGL_TRIANGLE_BUDGET = 4096;
/** Each 2D triangle is a save/clip/transform/drawImage. This is what still fits in a 60 fps frame. */
const CANVAS_TRIANGLE_BUDGET = 220;

class WebGLRigRenderer implements RigRenderer {
  readonly backend = 'webgl' as const;
  readonly downgradeReason = null;
  readonly triangleBudget = WEBGL_TRIANGLE_BUDGET;

  private readonly textures = new WeakMap<TexImageSource, WebGLTexture>();
  private readonly positionBuffer: WebGLBuffer;
  private readonly uvBuffer: WebGLBuffer;
  private readonly indexBuffer: WebGLBuffer;
  private readonly alphaLocation: WebGLUniformLocation;
  private readonly positionLocation: number;
  private readonly uvLocation: number;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly gl: WebGLRenderingContext,
    private readonly program: WebGLProgram,
  ) {
    this.positionBuffer = this.requireBuffer();
    this.uvBuffer = this.requireBuffer();
    this.indexBuffer = this.requireBuffer();
    this.positionLocation = gl.getAttribLocation(program, 'a_position');
    this.uvLocation = gl.getAttribLocation(program, 'a_uv');
    const alpha = gl.getUniformLocation(program, 'u_alpha');
    if (alpha === null) throw new Error('shader is missing u_alpha');
    this.alphaLocation = alpha;

    gl.useProgram(program);
    gl.enable(gl.BLEND);
    // Premultiplied source over: the canvas is transparent and soft fur edges must not fringe.
    gl.blendFuncSeparate(gl.ONE, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
  }

  private requireBuffer(): WebGLBuffer {
    const buffer = this.gl.createBuffer();
    if (buffer === null) throw new Error('out of WebGL buffers');
    return buffer;
  }

  private textureFor(image: TexImageSource): WebGLTexture {
    const cached = this.textures.get(image);
    if (cached) return cached;
    const { gl } = this;
    const texture = gl.createTexture();
    if (texture === null) throw new Error('out of WebGL textures');
    gl.bindTexture(gl.TEXTURE_2D, texture);
    // No mipmaps and clamped wrapping: the textures are not powers of two, and in WebGL 1 that is
    // the only combination that is allowed to filter at all.
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
    this.textures.set(image, texture);
    return texture;
  }

  resize(cssSize: number, devicePixelRatio: number): void {
    const pixels = Math.max(1, Math.round(cssSize * devicePixelRatio));
    if (this.canvas.width !== pixels || this.canvas.height !== pixels) {
      this.canvas.width = pixels;
      this.canvas.height = pixels;
    }
    this.canvas.style.width = `${cssSize}px`;
    this.canvas.style.height = `${cssSize}px`;
    this.gl.viewport(0, 0, pixels, pixels);
  }

  draw(calls: readonly DrawCall[]): void {
    const { gl } = this;
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.program);
    for (const call of calls) {
      if (call.alpha <= 0) continue;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.positionBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, call.positions, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(this.positionLocation);
      gl.vertexAttribPointer(this.positionLocation, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ARRAY_BUFFER, this.uvBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, call.uv, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(this.uvLocation);
      gl.vertexAttribPointer(this.uvLocation, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indexBuffer);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, call.indices, gl.STATIC_DRAW);

      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.textureFor(call.image));
      gl.uniform1f(this.alphaLocation, call.alpha);
      gl.drawElements(gl.TRIANGLES, call.indices.length, gl.UNSIGNED_SHORT, 0);
    }
  }

  dispose(): void {
    const { gl } = this;
    gl.deleteBuffer(this.positionBuffer);
    gl.deleteBuffer(this.uvBuffer);
    gl.deleteBuffer(this.indexBuffer);
    gl.deleteProgram(this.program);
  }
}

/** Grow a triangle about its centroid, in pixels, so neighbours overlap instead of showing a seam. */
const SEAM_OVERLAP_PX = 0.5;

class Canvas2DRigRenderer implements RigRenderer {
  readonly backend = 'canvas2d' as const;
  readonly triangleBudget = CANVAS_TRIANGLE_BUDGET;
  private size = 0;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly context: CanvasRenderingContext2D,
    readonly downgradeReason: string,
  ) {}

  resize(cssSize: number, devicePixelRatio: number): void {
    const pixels = Math.max(1, Math.round(cssSize * devicePixelRatio));
    if (this.canvas.width !== pixels || this.canvas.height !== pixels) {
      this.canvas.width = pixels;
      this.canvas.height = pixels;
    }
    this.canvas.style.width = `${cssSize}px`;
    this.canvas.style.height = `${cssSize}px`;
    this.size = pixels;
  }

  draw(calls: readonly DrawCall[]): void {
    const ctx = this.context;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.size, this.size);
    for (const call of calls) {
      if (call.alpha <= 0) continue;
      ctx.globalAlpha = call.alpha;
      const width = imageWidth(call.image);
      const height = imageHeight(call.image);
      for (let i = 0; i < call.indices.length; i += 3) {
        this.drawTriangle(call, i, width, height);
      }
    }
    ctx.globalAlpha = 1;
  }

  /**
   * One textured triangle: clip to it, then set the unique affine map that sends its three texture
   * corners onto its three screen corners and blit the whole image through that clip.
   */
  private drawTriangle(call: DrawCall, offset: number, width: number, height: number): void {
    const ctx = this.context;
    const a = call.indices[offset];
    const b = call.indices[offset + 1];
    const c = call.indices[offset + 2];
    const x0 = call.positions[a * 2] * this.size;
    const y0 = call.positions[a * 2 + 1] * this.size;
    const x1 = call.positions[b * 2] * this.size;
    const y1 = call.positions[b * 2 + 1] * this.size;
    const x2 = call.positions[c * 2] * this.size;
    const y2 = call.positions[c * 2 + 1] * this.size;
    const u0 = call.uv[a * 2] * width;
    const v0 = call.uv[a * 2 + 1] * height;
    const u1 = call.uv[b * 2] * width;
    const v1 = call.uv[b * 2 + 1] * height;
    const u2 = call.uv[c * 2] * width;
    const v2 = call.uv[c * 2 + 1] * height;

    const determinant = (u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0);
    if (determinant === 0) return;
    const inverse = 1 / determinant;
    const m11 = ((x1 - x0) * (v2 - v0) - (x2 - x0) * (v1 - v0)) * inverse;
    const m12 = ((y1 - y0) * (v2 - v0) - (y2 - y0) * (v1 - v0)) * inverse;
    const m21 = ((x2 - x0) * (u1 - u0) - (x1 - x0) * (u2 - u0)) * inverse;
    const m22 = ((y2 - y0) * (u1 - u0) - (y1 - y0) * (u2 - u0)) * inverse;

    const centroidX = (x0 + x1 + x2) / 3;
    const centroidY = (y0 + y1 + y2) / 3;
    ctx.save();
    ctx.beginPath();
    grow(ctx, x0, y0, centroidX, centroidY, true);
    grow(ctx, x1, y1, centroidX, centroidY, false);
    grow(ctx, x2, y2, centroidX, centroidY, false);
    ctx.closePath();
    ctx.clip();
    ctx.setTransform(m11, m12, m21, m22, x0 - m11 * u0 - m21 * v0, y0 - m12 * u0 - m22 * v0);
    ctx.drawImage(call.image as CanvasImageSource, 0, 0);
    ctx.restore();
  }

  dispose(): void {
    /* The 2D context owns nothing that has to be released by hand. */
  }
}

function grow(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  centroidX: number,
  centroidY: number,
  first: boolean,
): void {
  const dx = x - centroidX;
  const dy = y - centroidY;
  const length = Math.hypot(dx, dy) || 1;
  const px = x + (dx / length) * SEAM_OVERLAP_PX;
  const py = y + (dy / length) * SEAM_OVERLAP_PX;
  if (first) ctx.moveTo(px, py);
  else ctx.lineTo(px, py);
}

function imageWidth(image: TexImageSource): number {
  return 'naturalWidth' in image ? image.naturalWidth : (image as { width: number }).width;
}

function imageHeight(image: TexImageSource): number {
  return 'naturalHeight' in image ? image.naturalHeight : (image as { height: number }).height;
}

function compile(gl: WebGLRenderingContext, kind: number, source: string): WebGLShader {
  const shader = gl.createShader(kind);
  if (shader === null) throw new Error('cannot create shader');
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader) ?? 'unknown error';
    gl.deleteShader(shader);
    throw new Error(`shader did not compile: ${log}`);
  }
  return shader;
}

function linkProgram(gl: WebGLRenderingContext): WebGLProgram {
  const program = gl.createProgram();
  if (program === null) throw new Error('cannot create program');
  const vertex = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
  const fragment = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  gl.deleteShader(vertex);
  gl.deleteShader(fragment);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(program) ?? 'unknown error';
    gl.deleteProgram(program);
    throw new Error(`program did not link: ${log}`);
  }
  return program;
}

export class RigRendererError extends Error {}

/**
 * Pick a backend for this canvas. WebGL when it is there, Canvas 2D when it is not, and an error
 * only when neither can be had — at which point the pet has to fall back to plain sprite frames.
 */
export function createRigRenderer(canvas: HTMLCanvasElement): RigRenderer {
  let reason = 'WebGL is unavailable in this browser';
  try {
    const gl =
      canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: true }) ??
      canvas.getContext('experimental-webgl', { alpha: true, premultipliedAlpha: true });
    if (gl) return new WebGLRigRenderer(canvas, gl as WebGLRenderingContext, linkProgram(gl as WebGLRenderingContext));
  } catch (error) {
    reason = `WebGL failed to start: ${error instanceof Error ? error.message : String(error)}`;
  }
  const context = canvas.getContext('2d');
  if (context === null) throw new RigRendererError(`${reason}, and Canvas 2D is unavailable too`);
  return new Canvas2DRigRenderer(canvas, context, reason);
}
