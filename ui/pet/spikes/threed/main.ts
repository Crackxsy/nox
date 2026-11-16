/**
 * Prototype B, "3D skeletal": three.js, a rigged glTF quadruped with its authored Walk clip, plus
 * procedural layers on top - breathing, look-at on neck/head, posture poses (sit, lie, play-bow
 * stretch), jiggle bones for tail and ears, and a jaw driven by the audio envelope. The ear and
 * jaw bones do not exist in the asset; they are added at load time and vertices re-weighted.
 *
 * Stand-in model: Khronos glTF sample "Fox" (model CC0 by PixelMannen; rig & animation CC BY 4.0
 * by tomkranis; glTF conversion CC BY 4.0 by @AsoboStudio and @scurest). An Eulenfuchs would need
 * its own model, see the README.
 */
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { type Spike, type SpikeContext, boot } from '../shared/harness';
import { TALK_START, clamp, lerp, lookTarget, mulberry32, noise1, smooth, window } from '../shared/timeline';

const X = new THREE.Vector3(1, 0, 0);
const Y = new THREE.Vector3(0, 1, 0);
const Z = new THREE.Vector3(0, 0, 1);
const FOX_HEIGHT = 79; // model units, ground to ear tip
const REST_YAW = 0.38; // three-quarter view at rest
const WALK_NATIVE_SPEED = 140; // model units/s of the Walk clip at timeScale 1 (measured from foot slide)

interface Jiggle {
  bone: THREE.Object3D;
  tip: THREE.Vector3; // bone-local
  k: number;
  c: number;
  limit: number;
  p: THREE.Vector3;
  v: THREE.Vector3;
  init: boolean;
}

class ThreeD implements Spike {
  title = 'B · 3D-Skelett';
  subtitle = 'three.js · Platzhalter-Modell „Fox“ (Khronos, CC BY 4.0) – nicht der Eulenfuchs';
  gl!: WebGL2RenderingContext;
  private ctx!: SpikeContext;
  private renderer!: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera!: THREE.PerspectiveCamera;
  private fox!: THREE.Object3D;
  private mixer!: THREE.AnimationMixer;
  private walk!: THREE.AnimationAction;
  private bones = new Map<string, THREE.Bone>();
  private rest = new Map<THREE.Object3D, { p: THREE.Vector3; q: THREE.Quaternion; s: THREE.Vector3 }>();
  private jiggles: Jiggle[] = [];
  private foxScale = 1;
  private unitsPerH = 1;
  private look = new THREE.Vector2();
  private lookV = new THREE.Vector2();
  private breath = 0;
  private mouth = 0;
  private rng = mulberry32(7);
  private ground!: THREE.Mesh;
  private blob!: THREE.Mesh;

  async init(canvas: HTMLCanvasElement, ctx: SpikeContext): Promise<void> {
    this.ctx = ctx;
    const record = new URLSearchParams(location.search).get('mode') === 'record';
    this.renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, premultipliedAlpha: true, preserveDrawingBuffer: record });
    this.renderer.setPixelRatio(1);
    this.renderer.setSize(ctx.w, ctx.h, false);
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    this.gl = this.renderer.getContext() as WebGL2RenderingContext;

    // camera: slightly elevated, the ground origin lands on the shared ground line
    const dist = 560;
    this.camera = new THREE.PerspectiveCamera(24, ctx.w / ctx.h, 10, 3000);
    this.camera.position.set(0, 95, dist);
    this.camera.lookAt(0, 40, 0);
    this.camera.updateMatrixWorld();
    const pxPerUnit = ctx.h / (2 * dist * Math.tan(THREE.MathUtils.degToRad(12)));
    this.foxScale = (0.66 * ctx.H) / (FOX_HEIGHT * pxPerUnit);
    this.unitsPerH = ctx.H / pxPerUnit;
    const o = new THREE.Vector3(0, 0, 0).project(this.camera);
    const ox = ((o.x + 1) / 2) * ctx.w;
    const oy = ((1 - o.y) / 2) * ctx.h;
    this.camera.setViewOffset(ctx.w, ctx.h, ox - ctx.centerX, oy - ctx.groundY, ctx.w, ctx.h);

    this.scene.add(new THREE.HemisphereLight(0xfff1e0, 0x4a3628, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 2.6);
    sun.position.set(-160, 260, 220);
    sun.castShadow = true;
    sun.shadow.mapSize.set(1024, 1024);
    sun.shadow.camera.left = -160;
    sun.shadow.camera.right = 160;
    sun.shadow.camera.top = 160;
    sun.shadow.camera.bottom = -160;
    sun.shadow.radius = 6;
    sun.shadow.bias = -0.0005;
    this.scene.add(sun);
    const rim = new THREE.DirectionalLight(0xffd6a8, 1.4);
    rim.position.set(200, 120, -200);
    this.scene.add(rim);

    this.ground = new THREE.Mesh(new THREE.PlaneGeometry(2000, 2000), new THREE.ShadowMaterial({ opacity: 0.28 }));
    this.ground.rotation.x = -Math.PI / 2;
    this.ground.receiveShadow = true;
    this.scene.add(this.ground);
    // soft contact blob (directional shadows alone look detached on a transparent background)
    const blobTex = (() => {
      const c = document.createElement('canvas');
      c.width = c.height = 128;
      const g = c.getContext('2d')!;
      const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
      grad.addColorStop(0, 'rgba(0,0,0,0.55)');
      grad.addColorStop(1, 'rgba(0,0,0,0)');
      g.fillStyle = grad;
      g.fillRect(0, 0, 128, 128);
      return new THREE.CanvasTexture(c);
    })();
    this.blob = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ map: blobTex, transparent: true, depthWrite: false }));
    this.blob.rotation.x = -Math.PI / 2;
    this.blob.position.y = 0.2;
    this.scene.add(this.blob);

    const gltf = await new GLTFLoader().loadAsync('/fox/Fox.glb');
    this.fox = gltf.scene;
    this.fox.scale.setScalar(this.foxScale);
    this.scene.add(this.fox);
    let mesh: THREE.SkinnedMesh | null = null;
    this.fox.traverse((n) => {
      if ((n as THREE.Bone).isBone) this.bones.set(n.name, n as THREE.Bone);
      if ((n as THREE.SkinnedMesh).isSkinnedMesh) mesh = n as THREE.SkinnedMesh;
    });
    const skinned = mesh as unknown as THREE.SkinnedMesh;
    skinned.castShadow = true;
    // smooth normals: the asset ships without normals and would render faceted
    skinned.geometry.computeVertexNormals();
    const mat = skinned.material as THREE.MeshStandardMaterial;
    mat.flatShading = false;
    mat.roughness = 0.85;
    mat.needsUpdate = true;

    this.fox.updateMatrixWorld(true);
    this.addBones(skinned);
    for (const b of this.bones.values()) this.rest.set(b, { p: b.position.clone(), q: b.quaternion.clone(), s: b.scale.clone() });

    this.mixer = new THREE.AnimationMixer(this.fox);
    this.walk = this.mixer.clipAction(gltf.animations.find((c) => c.name === 'Walk')!);
    this.walk.play();
    this.walk.setEffectiveWeight(0);

    const J = (name: string, tip: [number, number, number], k: number, c: number, limit: number): Jiggle => ({
      bone: this.bones.get(name)!, tip: new THREE.Vector3(...tip), k, c, limit, p: new THREE.Vector3(), v: new THREE.Vector3(), init: false,
    });
    const tailTip = (name: string): [number, number, number] => {
      const child = this.bones.get(name)!.children.find((ch) => (ch as THREE.Bone).isBone);
      return child ? (child.position.toArray() as [number, number, number]) : [0, 18, 0];
    };
    this.jiggles = [
      J('b_Tail01_012', tailTip('b_Tail01_012'), 110, 9, 0.6),
      J('b_Tail02_013', tailTip('b_Tail02_013'), 80, 7, 0.7),
      J('b_Tail03_014', [0, 20, 0], 60, 6, 0.8),
      J('ear_l', this.earTip.l, 300, 11, 0.5),
      J('ear_r', this.earTip.r, 300, 11, 0.5),
    ];
  }

  private earTip = { l: [0, 1, 0] as [number, number, number], r: [0, 1, 0] as [number, number, number] };

  /** Add ear and jaw bones under the head and move the matching vertices onto them. */
  private addBones(mesh: THREE.SkinnedMesh): void {
    const head = this.bones.get('b_Head_05')!;
    const sk = mesh.skeleton;
    const geo = mesh.geometry;
    const P = geo.attributes.position as THREE.BufferAttribute;
    const SI = geo.attributes.skinIndex as THREE.BufferAttribute;
    const SW = geo.attributes.skinWeight as THREE.BufferAttribute;
    const toModel = (v: THREE.Vector3) => this.fox.worldToLocal(v.clone());
    const make = (name: string, modelPos: THREE.Vector3): number => {
      const b = new THREE.Bone();
      b.name = name;
      head.add(b);
      const world = this.fox.localToWorld(modelPos.clone());
      b.position.copy(head.worldToLocal(world));
      b.updateMatrixWorld(true);
      sk.bones.push(b);
      sk.boneInverses.push(b.matrixWorld.clone().invert().multiply(mesh.matrixWorld));
      this.bones.set(name, b);
      return sk.bones.length - 1;
    };
    // vertex positions are in mesh space == model space for this asset (identity bind matrix)
    const earL = make('ear_l', new THREE.Vector3(8, 67, 46));
    const earR = make('ear_r', new THREE.Vector3(-8, 67, 46));
    const jaw = make('jaw', new THREE.Vector3(0, 54, 44));
    const tipL = this.bones.get('ear_l')!.worldToLocal(this.fox.localToWorld(new THREE.Vector3(13, 79, 51)));
    const tipR = this.bones.get('ear_r')!.worldToLocal(this.fox.localToWorld(new THREE.Vector3(-13, 79, 51)));
    this.earTip = { l: tipL.toArray() as [number, number, number], r: tipR.toArray() as [number, number, number] };
    const v = new THREE.Vector3();
    for (let i = 0; i < P.count; i++) {
      v.fromBufferAttribute(P, i);
      let bone = -1;
      if (v.y > 72 && v.z > 44) bone = v.x > 0 ? earL : earR; // ear tips
      else if (v.z > 47 && v.y < 54.5) bone = jaw; // lower muzzle
      if (bone < 0) continue;
      SI.setXYZW(i, bone, 0, 0, 0);
      SW.setXYZW(i, 1, 0, 0, 0);
    }
    SI.needsUpdate = SW.needsUpdate = true;
    void toModel;
    sk.boneTexture?.dispose();
    sk.boneTexture = null;
    const fresh = new THREE.Skeleton(sk.bones, sk.boneInverses);
    mesh.bind(fresh, mesh.bindMatrix);
  }

  reset(): void {
    this.breath = 0;
    this.mouth = 0;
    this.look.set(0, 0);
    this.lookV.set(0, 0);
    this.rng = mulberry32(7);
    for (const j of this.jiggles) j.init = false;
  }

  /** Rotate a bone about an axis given in the fox's model space (pre-multiplied in parent space). */
  private rot(name: string, axis: THREE.Vector3, angle: number): void {
    if (angle === 0) return;
    const b = this.bones.get(name)!;
    const parentQ = new THREE.Quaternion();
    b.parent!.getWorldQuaternion(parentQ);
    const foxQ = this.fox.getWorldQuaternion(new THREE.Quaternion());
    const pm = foxQ.clone().invert().multiply(parentQ); // parent orientation in model space
    const qm = new THREE.Quaternion().setFromAxisAngle(axis, angle);
    const local = pm.clone().invert().multiply(qm).multiply(pm);
    b.quaternion.premultiply(local);
    b.updateMatrixWorld(true);
  }

  frame(t: number, dt: number): void {
    const { ctx } = this;
    // ---- reset to rest, then authored clip -------------------------------------------------
    for (const [b, r] of this.rest) {
      b.position.copy(r.p);
      b.quaternion.copy(r.q);
      b.scale.copy(r.s);
    }

    // walk plan (shared timing with the hop phase of the 2D prototypes)
    const D = 0.5 * this.unitsPerH;
    const out = window(t, 6.5, 6.85, 7.75, 8.25);
    const back = window(t, 7.85, 8.3, 9.1, 9.5);
    const walking = Math.max(window(t, 6.6, 6.9, 7.8, 8.0), window(t, 8.15, 8.35, 9.15, 9.4));
    let x = 0;
    if (t >= 6.85 && t < 7.8) x = D * smooth(6.85, 7.8, t);
    else if (t >= 7.8 && t < 8.3) x = D;
    else if (t >= 8.3 && t < 9.25) x = D * (1 - smooth(8.3, 9.25, t));
    // yaw: rest three-quarter -> face right -> turn through the camera -> face left -> rest
    let yaw = REST_YAW;
    yaw = lerp(yaw, Math.PI / 2, out);
    yaw = lerp(yaw, -Math.PI / 2, smooth(7.8, 8.3, t) * back + 0 * back);
    yaw = lerp(yaw, REST_YAW, smooth(9.2, 9.6, t));
    this.walk.setEffectiveWeight(walking);
    this.walk.setEffectiveTimeScale(D / 0.95 / (WALK_NATIVE_SPEED * this.foxScale));
    if (dt === 0) this.mixer.setTime(0);
    this.mixer.update(dt);

    // ---- posture: sit -> lie -> sleep -> play-bow -> happy -----------------------------------------
    const sit = window(t, 9.55, 10.15, 13.1, 13.5);
    const lie = window(t, 10.1, 10.9, 13.0, 13.45);
    const asleep = window(t, 10.8, 11.4, 13.0, 13.3);
    const bow = window(t, 13.35, 13.65, 13.85, 14.15);
    const happy = window(t, 14.0, 14.2, 14.9, 15.2);
    const talking = t >= TALK_START - 0.1;

    this.fox.position.set(x, 0, 0);
    this.fox.rotation.set(0, yaw, 0);
    this.fox.updateMatrixWorld(true);

    const hip = 'b_Hip_01';
    // Posture poses: [bone, axis, angle] in model space, blended by weight. Lying replaces sitting.
    type Pose = [string, THREE.Vector3, number][];
    const SIT: Pose = [
      [hip, X, -0.55], ['b_LeftLeg01_015', X, -0.9], ['b_RightLeg01_019', X, -0.9],
      ['b_LeftLeg02_016', X, 1.5], ['b_RightLeg02_020', X, 1.5], ['b_LeftFoot01_017', X, -0.9],
      ['b_RightFoot01_021', X, -0.9], ['b_LeftUpperArm_09', X, 0.55], ['b_RightUpperArm_06', X, 0.55],
      ['b_Neck_04', X, 0.3],
    ];
    const LIE: Pose = [
      [hip, X, 0.04], ['b_LeftLeg01_015', X, -1.25], ['b_RightLeg01_019', X, -1.25],
      ['b_LeftLeg02_016', X, 2.3], ['b_RightLeg02_020', X, 2.3], ['b_LeftFoot01_017', X, -1.0],
      ['b_RightFoot01_021', X, -1.0], ['b_LeftUpperArm_09', X, -1.4], ['b_RightUpperArm_06', X, -1.4],
      ['b_LeftForeArm_010', X, -0.1], ['b_RightForeArm_07', X, -0.1], ['b_LeftHand_011', X, 0.4],
      ['b_RightHand_08', X, 0.4], ['b_Neck_04', X, 0.45], ['b_Tail01_012', Y, 1.0], ['b_Tail02_013', Y, 1.1],
      ['b_Tail03_014', Y, 1.0], ['b_Tail01_012', X, 0.5],
    ];
    const BOW: Pose = [
      ['b_Spine01_02', X, 0.32], ['b_LeftUpperArm_09', X, -0.95], ['b_RightUpperArm_06', X, -0.95],
      ['b_LeftForeArm_010', X, 0.25], ['b_RightForeArm_07', X, 0.25], ['b_Neck_04', X, -0.55],
      ['b_Tail01_012', X, -0.5],
    ];
    const apply = (pose: Pose, w: number) => {
      if (w > 0) for (const [n, ax, a] of pose) this.rot(n, ax, a * w);
    };
    const wLie = lie;
    const wSit = sit * (1 - lie);
    apply(SIT, wSit);
    apply(LIE, wLie);
    apply(BOW, bow);
    this.rot('b_Head_05', X, 0.3 * asleep);
    this.rot('b_Head_05', Z, 0.3 * asleep);
    const rootDrop = 11 * wSit + 31 * wLie + 4 * bow;
    this.fox.position.y = -rootDrop * this.foxScale;

    // ---- breathing, idle life ----------------------------------------------------------------
    this.breath += 2 * Math.PI * lerp(0.32, 0.2, asleep) * dt;
    const b = Math.sin(this.breath) * (1 + 1.2 * asleep);
    const sp = this.bones.get('b_Spine02_03')!;
    sp.scale.multiplyScalar(1 + 0.012 * b);
    this.rot('b_Neck_04', X, -0.02 * b);
    this.rot(hip, Z, 0.03 * noise1(t * 0.35, 1) * (1 - lie));
    this.rot('b_Tail01_012', Y, 0.18 * Math.sin(2 * Math.PI * 0.3 * t) * (1 - lie));
    this.rot('b_Tail02_013', Y, 0.2 * Math.sin(2 * Math.PI * 0.3 * t - 0.8));

    // happy: hops on the spot, tail wags
    const hopY = Math.max(0, Math.sin(2 * Math.PI * 2.3 * (t - 14.0))) * happy;
    this.fox.position.y += hopY * 12 * this.foxScale;
    this.rot('b_Tail01_012', Y, 0.55 * Math.sin(2 * Math.PI * 3.3 * t) * happy);
    this.rot('b_Tail01_012', X, -0.4 * happy);

    // ---- ears & jaw (added bones) ---------------------------------------------------------------
    const twitch = (s: number) => (s < 0 || s > 0.45 ? 0 : s < 0.05 ? s / 0.05 : Math.exp(-(s - 0.05) * 9));
    this.rot('ear_l', Z, 0.5 * twitch(t - 1.25) + 0.35 * asleep);
    this.rot('ear_r', Z, -0.5 * twitch(t - 2.45) - 0.35 * asleep);
    this.rot('ear_l', X, -0.3 * happy);
    this.rot('ear_r', X, -0.3 * happy);
    const raw = talking ? ctx.envelope.at(t - TALK_START) : 0;
    this.mouth += (raw - this.mouth) * (1 - Math.exp(-dt / (raw > this.mouth ? 0.025 : 0.07)));
    if (dt === 0) this.mouth = 0;
    const yawn = window(t, 13.1, 13.35, 13.6, 13.95);
    const jaw = Math.max(this.mouth, yawn * 1.2);
    this.rot('jaw', X, 0.32 * jaw);
    this.rot('b_Head_05', X, -0.08 * jaw - 0.25 * yawn);

    // ---- look-at: neck and head aim at the target (eyes are painted, they cannot move) ---------
    const lt = lookTarget(t);
    const head = this.bones.get('b_Head_05')!;
    const hp = head.getWorldPosition(new THREE.Vector3());
    let goal = new THREE.Vector2(0, 0);
    // default: look at the viewer
    const cam = this.camera.position.clone();
    const toCam = this.fox.worldToLocal(cam.clone()).sub(this.fox.worldToLocal(hp.clone()));
    const camYaw = Math.atan2(toCam.x, toCam.z);
    const camPitch = Math.atan2(toCam.y, Math.hypot(toCam.x, toCam.z));
    goal.set(camYaw, camPitch);
    if (lt.w > 0) {
      const ndc = new THREE.Vector3((lt.u * 2 - 1), -(lt.v * 2 - 1), hp.clone().project(this.camera).z);
      const world = ndc.unproject(this.camera);
      const d = this.fox.worldToLocal(world.clone()).sub(this.fox.worldToLocal(hp.clone()));
      const g2 = new THREE.Vector2(Math.atan2(d.x, d.z), Math.atan2(d.y, Math.hypot(d.x, d.z)));
      goal.lerp(g2, lt.w);
    }
    if (walking > 0) goal.lerp(new THREE.Vector2(0, -0.1), walking);
    goal.lerp(new THREE.Vector2(0, 0), Math.max(lie, bow));
    goal.x = clamp(goal.x, -1.0, 1.0);
    goal.y = clamp(goal.y, -0.5, 0.6);
    if (dt === 0) this.look.copy(goal);
    const w0 = 7;
    for (const k of ['x', 'y'] as const) {
      const a = w0 * w0 * (goal[k] - this.look[k]) - 2 * w0 * this.lookV[k];
      this.lookV[k] += a * dt;
      this.look[k] += this.lookV[k] * dt;
    }
    if (talking) this.look.y += 0.04 * noise1(t * 1.3, 4);
    this.rot('b_Neck_04', Y, 0.45 * this.look.x);
    this.rot('b_Head_05', Y, 0.55 * this.look.x);
    this.rot('b_Neck_04', X, -0.4 * this.look.y);
    this.rot('b_Head_05', X, -0.6 * this.look.y);

    // ---- jiggle bones ----------------------------------------------------------------------------------
    this.fox.updateMatrixWorld(true);
    const SUB = 1 / 240;
    for (const j of this.jiggles) {
      const pivot = j.bone.getWorldPosition(new THREE.Vector3());
      const target = j.bone.localToWorld(j.tip.clone());
      if (!j.init || dt === 0) {
        j.p.copy(target);
        j.v.set(0, 0, 0);
        j.init = true;
      }
      const len = target.distanceTo(pivot);
      const steps = Math.max(1, Math.round(dt / SUB));
      const h = dt / steps;
      for (let s = 0; s < steps && dt > 0; s++) {
        const a = target.clone().sub(j.p).multiplyScalar(j.k).addScaledVector(j.v, -j.c);
        j.v.addScaledVector(a, h);
        j.p.addScaledVector(j.v, h);
        j.p.sub(pivot).setLength(len).add(pivot);
      }
      const from = target.clone().sub(pivot).normalize();
      const to = j.p.clone().sub(pivot).normalize();
      let q = new THREE.Quaternion().setFromUnitVectors(from, to);
      const ang = 2 * Math.acos(clamp(q.w, -1, 1));
      if (ang > j.limit) q = new THREE.Quaternion().slerp(q, j.limit / ang);
      const parentQ = j.bone.parent!.getWorldQuaternion(new THREE.Quaternion());
      const local = parentQ.clone().invert().multiply(q).multiply(parentQ);
      j.bone.quaternion.premultiply(local);
      j.bone.updateMatrixWorld(true);
    }

    // contact blob follows the body
    const hipW = this.bones.get(hip)!.getWorldPosition(new THREE.Vector3());
    this.blob.position.set(hipW.x * 0.5 + x * 0.5, 0.2, hipW.z * 0.5);
    const lift = this.fox.position.y / this.foxScale;
    this.blob.scale.set(150 * this.foxScale * (1 - lift / 60), 1, 70 * this.foxScale * (1 - lift / 60));
    (this.blob.material as THREE.MeshBasicMaterial).opacity = 1 - clamp(lift / 30);

    this.renderer.render(this.scene, this.camera);
    void this.rng;
  }
}

void boot(new ThreeD());
