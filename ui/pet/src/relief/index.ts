/**
 * Public surface of the relief ("2.5D") renderer: the creature's painted art cut into parts, a bone
 * hierarchy with spring physics under them, and a relief height per part so head and body turns
 * are real 3D rotations with matching light. A variant opts in through `rig.json` ("relief");
 * every other variant keeps the older rig renderer.
 */

export { ReliefBrain, hopShape } from './brain';
export type { Face } from './brain';
export { buildGeometry, skinPoint, weightFn } from './mesh';
export type { Pixels, ReliefGeometry } from './mesh';
export { loadRelief, parseReliefDefinition, ReliefDefinitionError } from './reliefFile';
export type { LoadedRelief, ReliefDefinition } from './reliefFile';
export { ReliefRenderer, ReliefRendererError } from './renderer';
export { Skeleton, SkeletonError } from './skeleton';
export { SpeechEnvelope } from './speech';
export { reliefTargets } from './stateMapping';
export type { ReliefTargets } from './stateMapping';
export { PoseSwitch, SWITCH_MS } from './transition';
