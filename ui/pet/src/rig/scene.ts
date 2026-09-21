/**
 * The scene: skeleton, meshes, player and renderer wired into one object with a single `frame()`.
 *
 * Kept out of React on purpose. A rigged pet redraws up to sixty times a second and React has
 * nothing useful to say about any of those frames — the component owns the canvas and the
 * lifecycle, this owns the drawing. It is also what makes the rig testable: every input to
 * `frame()` is a number, and every output is a draw call.
 *
 * Mesh resolution is decided here rather than taken from the file, because the Canvas 2D fallback
 * pays per triangle. A rig that asks for a 20 x 20 grid gets it on WebGL and a coarser one on the
 * fallback, and `meshScale` says which happened.
 */

import { applyMatrix, buildSkeleton, poseMatrices } from './bones';
import type { Skeleton } from './bones';
import { buildGrid, closeLid, skin } from './mesh';
import type { Mesh } from './mesh';
import { RigPlayer } from './player';
import type { DrawCall, RigRenderer } from './renderer';
import type { LoadedRig } from './rigFile';
import type { Layer, Matrix2D, Pose } from './types';

interface SceneLayer {
  layer: Layer;
  mesh: Mesh;
  positions: Float32Array;
  image: TexImageSource;
  lidBoneIndex: number;
}

/** Milliseconds a key-pose cross-dissolve takes. Long enough to read as a move, not a cut. */
export const POSE_CROSSFADE_MS = 260;

export class RigScene {
  readonly player: RigPlayer;
  readonly skeleton: Skeleton;
  /** Fraction of the requested grid density actually used; 1 on WebGL, less on the 2D fallback. */
  readonly meshScale: number;
  /** Key poses named in `rig.json` whose art is not there yet. Surfaced, never silently skipped. */
  readonly missingPoses: readonly string[];

  private readonly baseMesh: Mesh;
  private readonly basePositions: Float32Array;
  private readonly layers: SceneLayer[];
  private readonly calls: DrawCall[] = [];

  private currentPose: string | null = null;
  private previousPose: string | null = null;
  private dissolveStartMs = 0;

  constructor(
    private readonly loaded: LoadedRig,
    private readonly renderer: RigRenderer,
  ) {
    const { definition } = loaded;
    this.skeleton = buildSkeleton(definition.bones);
    this.player = new RigPlayer(definition.clips);
    this.missingPoses = loaded.missingPoses;

    const requested = definition.mesh.columns * definition.mesh.rows * 2;
    this.meshScale = Math.min(1, Math.sqrt(renderer.triangleBudget / Math.max(1, requested)));
    const columns = Math.max(2, Math.round(definition.mesh.columns * this.meshScale));
    const rows = Math.max(2, Math.round(definition.mesh.rows * this.meshScale));

    this.baseMesh = buildGrid([0, 0, 1, 1], columns, rows, this.skeleton);
    this.basePositions = new Float32Array(this.baseMesh.vertexCount * 2);
    this.layers = definition.layers.map((layer) => {
      const mesh = buildGrid(layer.rect, layer.grid.columns, layer.grid.rows, this.skeleton);
      const image = loaded.images[layer.file];
      if (!image) throw new Error(`layer "${layer.name}" has no loaded texture`);
      return {
        layer,
        mesh,
        positions: new Float32Array(mesh.vertexCount * 2),
        image,
        lidBoneIndex: layer.lid ? (this.skeleton.indexOf.get(layer.lid.bone) ?? -1) : -1,
      };
    });
  }

  /** Key poses whose art loaded and can therefore be dissolved to. */
  get availablePoses(): string[] {
    return Object.keys(this.loaded.definition.poses).filter(
      (name) => this.loaded.images[this.loaded.definition.poses[name].file] !== undefined,
    );
  }

  /**
   * Cross-dissolve to a named key pose while the mesh keeps deforming.
   *
   * Returns false when the pose has no art yet — the caller then knows to stay where it is rather
   * than to believe the creature moved.
   */
  setPose(name: string | null, nowMs: number): boolean {
    if (name !== null && !this.availablePoses.includes(name)) return false;
    if (name === this.currentPose) return true;
    this.previousPose = this.currentPose;
    this.currentPose = name;
    this.dissolveStartMs = nowMs;
    return true;
  }

  resize(cssSize: number, devicePixelRatio: number): void {
    this.renderer.resize(cssSize, devicePixelRatio);
  }

  /** Texture for a pose name, or the rig's base texture for `null`. */
  private textureFor(pose: string | null): TexImageSource {
    if (pose === null) return this.loaded.images[this.loaded.definition.base];
    return this.loaded.images[this.loaded.definition.poses[pose].file];
  }

  /**
   * Advance to `nowMs` and draw one frame.
   *
   * `lidClosure` is the floor under whatever the blink clip is doing, so a sleeping pet keeps its
   * eyes shut while still breathing.
   */
  frame(nowMs: number, lidClosure: number): void {
    const pose: Pose = this.player.update(nowMs);
    const matrices = poseMatrices(this.skeleton, pose);
    skin(this.baseMesh, matrices, this.basePositions);

    this.calls.length = 0;
    const dissolve =
      this.previousPose === this.currentPose
        ? 1
        : Math.min(1, (nowMs - this.dissolveStartMs) / POSE_CROSSFADE_MS);
    if (dissolve < 1) {
      this.calls.push({
        image: this.textureFor(this.previousPose),
        positions: this.basePositions,
        uv: this.baseMesh.uv,
        indices: this.baseMesh.indices,
        alpha: 1 - dissolve,
      });
    } else {
      this.previousPose = this.currentPose;
    }
    this.calls.push({
      image: this.textureFor(this.currentPose),
      positions: this.basePositions,
      uv: this.baseMesh.uv,
      indices: this.baseMesh.indices,
      alpha: dissolve,
    });

    for (const entry of this.layers) {
      skin(entry.mesh, matrices, entry.positions);
      if (entry.lidBoneIndex >= 0) {
        this.applyLid(entry, matrices, pose, lidClosure);
      }
      this.calls.push({
        image: entry.image,
        positions: entry.positions,
        uv: entry.mesh.uv,
        indices: entry.mesh.indices,
        alpha: 1,
      });
    }
    this.renderer.draw(this.calls);
  }

  private applyLid(
    entry: SceneLayer,
    matrices: readonly Matrix2D[],
    pose: Pose,
    floor: number,
  ): void {
    const bone = this.skeleton.bones[entry.lidBoneIndex];
    const openness = pose[bone.name]?.scaleY ?? 1;
    const amount = Math.max(floor, 1 - openness);
    if (amount <= 0) return;
    // The lid shuts onto the bone's own pivot, put through that bone's matrix, so a head that has
    // tilted or leaned takes its eyelids with it.
    const [, lidY] = applyMatrix(matrices[entry.lidBoneIndex], bone.pivot[0], bone.pivot[1]);
    closeLid(entry.positions, lidY, amount);
  }

  dispose(): void {
    this.renderer.dispose();
  }
}
