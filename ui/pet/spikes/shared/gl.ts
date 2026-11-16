/** Tiny WebGL2 helpers shared by the 2D and 2.5D renderers. */

export function program(gl: WebGL2RenderingContext, vs: string, fs: string): WebGLProgram {
  const sh = (type: number, src: string): WebGLShader => {
    const s = gl.createShader(type)!;
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? 'shader');
    return s;
  };
  const p = gl.createProgram()!;
  gl.attachShader(p, sh(gl.VERTEX_SHADER, vs));
  gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) ?? 'link');
  return p;
}

/** Upload with premultiplied alpha and mipmaps (straight-alpha PNG in, premultiplied texels out). */
export function texture(gl: WebGL2RenderingContext, img: TexImageSource, mip = true): WebGLTexture {
  const t = gl.createTexture()!;
  gl.bindTexture(gl.TEXTURE_2D, t);
  // ImageBitmaps arrive premultiplied (createImageBitmap option); other sources are premultiplied here.
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, !(img instanceof ImageBitmap));
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  if (mip) {
    gl.generateMipmap(gl.TEXTURE_2D);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
  } else gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  return t;
}

/** A VAO with a dynamic float attribute set and static uv + indices. */
export class DynMesh {
  readonly vao: WebGLVertexArrayObject;
  private readonly pos: WebGLBuffer;
  readonly count: number;

  constructor(
    private readonly gl: WebGL2RenderingContext,
    posData: Float32Array,
    posSize: number,
    uv: Float32Array,
    indices: Uint16Array,
    extra?: { data: Float32Array; size: number; loc: number },
  ) {
    this.vao = gl.createVertexArray()!;
    gl.bindVertexArray(this.vao);
    this.pos = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.pos);
    gl.bufferData(gl.ARRAY_BUFFER, posData, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, posSize, gl.FLOAT, false, 0, 0);
    const ub = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, ub);
    gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);
    if (extra) {
      const eb = gl.createBuffer()!;
      gl.bindBuffer(gl.ARRAY_BUFFER, eb);
      gl.bufferData(gl.ARRAY_BUFFER, extra.data, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(extra.loc);
      gl.vertexAttribPointer(extra.loc, extra.size, gl.FLOAT, false, 0, 0);
      this.extraBuf = eb;
    }
    const ib = gl.createBuffer()!;
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);
    gl.bindVertexArray(null);
    this.count = indices.length;
  }

  extraBuf?: WebGLBuffer;

  update(posData: Float32Array, extra?: Float32Array): void {
    const gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.pos);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, posData);
    if (extra && this.extraBuf) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.extraBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, extra);
    }
  }

  draw(): void {
    const gl = this.gl;
    gl.bindVertexArray(this.vao);
    gl.drawElements(gl.TRIANGLES, this.count, gl.UNSIGNED_SHORT, 0);
  }
}

/**
 * Eye shader: the iris slides inside the eye-ball ellipse (saccades), the upper lid closes along
 * the ellipse (what is hidden shows the eyeless facial disc of the base art underneath) and a dark
 * lash line rides on the lid edge.
 */
/**
 * Eye shader: the iris slides inside the eye-ball ellipse (saccades); the upper lid is the fur
 * just above the eye, pulled down over the ball, with a dark lash line on its edge; a lower lid
 * rises for "smiling" eyes. The eye texture is an opaque patch over the base art.
 */
export const EYE_FS = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_tex;
uniform sampler2D u_closed;
uniform vec2 u_c, u_r, u_iris;
uniform float u_lid, u_lower;
uniform float u_light;
out vec4 o;
void main(){
  vec2 p = (v_uv - u_c) / u_r;
  float d = length(p);
  // the lid region reaches a little past the ball so a closed lid also covers the dark rim
  float ins = 1.0 - smoothstep(1.02, 1.24, d);
  vec4 col = texture(u_tex, v_uv - u_iris * (1.0 - smoothstep(0.55, 0.95, d)));
  float xs = clamp(p.x / 1.25, -1.0, 1.0);
  float arc = sqrt(max(0.0, 1.0 - xs * xs));
  float yl = mix(-1.25, 0.95, u_lid) * arc;      // upper lid edge
  float yb = mix(1.25, 0.1, u_lower) * arc;      // lower lid edge
  float up = (1.0 - smoothstep(yl - 0.06, yl + 0.06, p.y)) * ins;
  float lo = smoothstep(yb - 0.06, yb + 0.06, p.y) * ins * step(0.001, u_lower);
  // lid skin: the eye patch with the ball inpainted from its own fur (made by the cutter)
  vec3 skin = texture(u_closed, v_uv).rgb;
  vec3 upper = skin * (0.94 - 0.2 * exp(-max(0.0, yl - p.y) / 0.14));
  vec3 lower = skin * (0.97 - 0.12 * exp(-max(0.0, p.y - yb) / 0.14));
  // the lid casts a soft shadow onto the ball
  float sh = 1.0 - 0.4 * ins * step(yl, p.y) * exp(-(p.y - yl) / 0.22) * smoothstep(0.0, 0.25, u_lid);
  vec3 rgb = col.rgb * sh;
  rgb = mix(rgb, upper, up);
  rgb = mix(rgb, lower, lo * (1.0 - up));
  float lash = exp(-pow((p.y - yl) / 0.08, 2.0)) * (1.0 - smoothstep(0.95, 1.15, d)) * smoothstep(0.02, 0.2, u_lid) * 0.85;
  float lashB = exp(-pow((p.y - yb) / 0.07, 2.0)) * (1.0 - smoothstep(0.9, 1.1, d)) * smoothstep(0.02, 0.3, u_lower) * 0.5 * (1.0 - up);
  rgb = mix(rgb, vec3(0.11, 0.065, 0.045) * col.a, max(lash, lashB));
  o = vec4(rgb * u_light, col.a);
}`;

export const SHADOW_FS = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform float u_alpha;
out vec4 o;
void main(){
  vec2 p = v_uv * 2.0 - 1.0;
  float r = dot(p, p);
  float a = u_alpha * exp(-r * 3.2) * (1.0 - smoothstep(0.7, 1.0, r));
  o = vec4(0.0, 0.0, 0.0, a);
}`;
