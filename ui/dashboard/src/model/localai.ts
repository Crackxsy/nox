/**
 * The local-model setup: the `ai.setup.status` / `ai.setup.check` / `ai.setup.start` payload.
 *
 * Narrowed by hand through `shared/guards` like `day.ts`: `ai.setup.*` is answered by the
 * `localai` extension, not by a pydantic model in `src/nox/ipc/protocol.py`. Nothing is
 * invented — a VRAM figure the core did not measure stays `null` and the tile says so, and a
 * failure reason arrives in the core's words.
 */

import { bool, isRecord, numOrNull, str, stringList } from '../../../shared/guards';

export interface LocalAiHardware {
  ramGb: number | null;
  /** `null` when there is no reading: only NVIDIA cards report VRAM (through `nvidia-smi`). */
  vramGb: number | null;
  machine: string;
  piGeneration: number | null;
}

export interface LocalAiRecommendation {
  /** `null` when this machine should not run a local model at all. */
  model: string | null;
  /** A stable code (`vram`, `ram_no_vram_reading`, `no_llm_pi`, ...). */
  reason: string;
  basis: string | null;
  downloadGb: number | null;
  measured: boolean;
}

export interface LocalAiOllama {
  reachable: boolean;
  installed: boolean;
  version: string;
  hasModel: boolean;
  /** A `package.install` tool exists, so the core can offer to install Ollama. */
  canInstall: boolean;
  instructions: string[];
}

export interface LocalAiProgress {
  /** Ollama's own status line ("pulling manifest", "verifying sha256 digest", ...). */
  status: string;
  completed: number;
  total: number;
}

export interface LocalAiProbe {
  textOk: boolean;
  toolOk: boolean;
  reason: string;
}

export interface LocalAiStatus {
  running: boolean;
  phase: string;
  /** `null` until a run (or a check) has finished. */
  outcome: string | null;
  detail: string;
  hardware: LocalAiHardware | null;
  recommendation: LocalAiRecommendation | null;
  ollama: LocalAiOllama | null;
  privacyMode: string;
  pullBlocked: boolean;
  progress: LocalAiProgress | null;
  probe: LocalAiProbe | null;
  modelWritten: string;
}

export const IDLE_LOCAL_AI: LocalAiStatus = {
  running: false,
  phase: 'idle',
  outcome: null,
  detail: '',
  hardware: null,
  recommendation: null,
  ollama: null,
  privacyMode: '',
  pullBlocked: false,
  progress: null,
  probe: null,
  modelWritten: '',
};

function parseHardware(raw: unknown): LocalAiHardware | null {
  if (!isRecord(raw)) return null;
  return {
    ramGb: numOrNull(raw.ram_gb),
    vramGb: numOrNull(raw.vram_gb),
    machine: str(raw.machine),
    piGeneration: numOrNull(raw.pi_generation),
  };
}

function parseRecommendation(raw: unknown): LocalAiRecommendation | null {
  if (!isRecord(raw)) return null;
  const model = str(raw.model);
  return {
    model: model || null,
    reason: str(raw.reason),
    basis: str(raw.basis) || null,
    downloadGb: numOrNull(raw.download_gb),
    measured: bool(raw.measured),
  };
}

function parseOllama(raw: unknown): LocalAiOllama | null {
  if (!isRecord(raw)) return null;
  return {
    reachable: bool(raw.reachable),
    installed: bool(raw.installed),
    version: str(raw.version),
    hasModel: bool(raw.has_model),
    canInstall: bool(raw.can_install),
    instructions: stringList(raw.instructions),
  };
}

function parseProgress(raw: unknown): LocalAiProgress | null {
  if (!isRecord(raw)) return null;
  return {
    status: str(raw.status),
    completed: numOrNull(raw.completed) ?? 0,
    total: numOrNull(raw.total) ?? 0,
  };
}

function parseProbe(raw: unknown): LocalAiProbe | null {
  if (!isRecord(raw)) return null;
  return { textOk: bool(raw.text_ok), toolOk: bool(raw.tool_ok), reason: str(raw.reason) };
}

/** Any `ai.setup.*` reply -> the tile's model; a malformed reply is the idle state, not a crash. */
export function parseLocalAiStatus(payload: unknown): LocalAiStatus {
  if (!isRecord(payload)) return IDLE_LOCAL_AI;
  return {
    running: bool(payload.running),
    phase: str(payload.phase, 'idle'),
    outcome: str(payload.outcome) || null,
    detail: str(payload.detail),
    hardware: parseHardware(payload.hardware),
    recommendation: parseRecommendation(payload.recommendation),
    ollama: parseOllama(payload.ollama),
    privacyMode: str(payload.privacy_mode),
    pullBlocked: bool(payload.pull_blocked),
    progress: parseProgress(payload.progress),
    probe: parseProbe(payload.probe),
    modelWritten: str(payload.model_written),
  };
}

/** 0..1 while the download size is known, `null` before Ollama has announced it. */
export function downloadFraction(progress: LocalAiProgress | null): number | null {
  if (!progress || progress.total <= 0) return null;
  return Math.min(1, progress.completed / progress.total);
}

/**
 * Whether "Einrichten" can do anything useful right now, and if not, why not is already on screen:
 * no model for this machine, Ollama not answering (and not installable from here), or a download
 * the privacy mode forbids.
 */
export function canStartSetup(status: LocalAiStatus): boolean {
  if (status.running || !status.recommendation?.model || !status.ollama) return false;
  if (!status.ollama.reachable) return false;
  return status.ollama.hasModel || !status.pullBlocked;
}
