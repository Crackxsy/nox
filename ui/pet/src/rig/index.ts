/**
 * Public surface of the 2D deformation rig.
 *
 * The rig turns one photograph into a creature: a triangulated mesh over the picture, a small bone
 * hierarchy under it, keyframed clips blended additively on top, and cut-out eye layers so a blink
 * is a lid and not a dip in brightness. A variant opts in by shipping `rig.json`; one that does
 * not keeps playing static frames exactly as before, and any rig error falls back to that path.
 */

export { AmbientScheduler } from './ambient';
export { applyMatrix, buildSkeleton, localMatrix, multiply, poseMatrices, SkeletonError } from './bones';
export type { Skeleton } from './bones';
export { accumulate, clipPhase, EASINGS, isEasingName, sampleClip, sampleTrack } from './clips';
export { buildGrid, buildLayerMesh, closeLid, computeWeights, influenceAt, skin } from './mesh';
export type { Mesh } from './mesh';
export { RigPlayer } from './player';
export type { PlayOptions } from './player';
export { createRigRenderer, RigRendererError } from './renderer';
export type { DrawCall, RigRenderer } from './renderer';
export { loadRig, parseRigDefinition, RigDefinitionError, rigFiles, rigUrl } from './rigFile';
export type { LoadedRig, RigLoadOptions } from './rigFile';
export { POSE_CROSSFADE_MS, RigScene } from './scene';
export { isRestingState, planFor, RIG_CLIPS } from './stateMapping';
export type { AmbientLevel, RigClipName, RigPlan, SustainedClip } from './stateMapping';
export * from './types';
