/**
 * The voice-model download: the `voice.model.status` / `voice.model.download` payload.
 *
 * Narrowed by hand through `shared/guards` like `localai.ts`: `voice.model.*` is answered by the
 * core's `voice` extension (`src/nox/voice/service.py`), not by a pydantic model in
 * `src/nox/ipc/protocol.py`. A size the core did not send stays 0 and is not shown; a failure
 * reason arrives in the core's words.
 */

import { bool, isRecord, numOrNull, str, stringList } from '../../../shared/guards';

export interface VoiceModelProgress {
  file: string;
  done: number;
  total: number;
  index: number;
  count: number;
}

export interface VoiceModelStatus {
  /** The engine whose files this is about (Supertonic unless Kokoro is configured). */
  engine: string;
  configuredEngine: string;
  directory: string;
  files: number;
  missing: string[];
  present: boolean;
  sizeBytes: number;
  missingBytes: number;
  license: string;
  running: boolean;
  /** `done` | `already_present` | `privacy_blocked` | `failed`, or `null` before any run. */
  outcome: string | null;
  detail: string;
  progress: VoiceModelProgress | null;
  privacyMode: string;
  downloadBlocked: boolean;
  restartRequired: boolean;
}

export const IDLE_VOICE_MODEL: VoiceModelStatus = {
  engine: '',
  configuredEngine: '',
  directory: '',
  files: 0,
  missing: [],
  present: false,
  sizeBytes: 0,
  missingBytes: 0,
  license: '',
  running: false,
  outcome: null,
  detail: '',
  progress: null,
  privacyMode: '',
  downloadBlocked: false,
  restartRequired: false,
};

function parseProgress(raw: unknown): VoiceModelProgress | null {
  if (!isRecord(raw)) return null;
  return {
    file: str(raw.file),
    done: numOrNull(raw.done) ?? 0,
    total: numOrNull(raw.total) ?? 0,
    index: numOrNull(raw.index) ?? 0,
    count: numOrNull(raw.count) ?? 0,
  };
}

/** Any `voice.model.*` reply -> the tile's model; a malformed reply is the idle state. */
export function parseVoiceModelStatus(payload: unknown): VoiceModelStatus {
  if (!isRecord(payload)) return IDLE_VOICE_MODEL;
  return {
    engine: str(payload.engine),
    configuredEngine: str(payload.configured_engine),
    directory: str(payload.directory),
    files: numOrNull(payload.files) ?? 0,
    missing: stringList(payload.missing),
    present: bool(payload.present),
    sizeBytes: numOrNull(payload.size_bytes) ?? 0,
    missingBytes: numOrNull(payload.missing_bytes) ?? 0,
    license: str(payload.license),
    running: bool(payload.running),
    outcome: str(payload.outcome) || null,
    detail: str(payload.detail),
    progress: parseProgress(payload.progress),
    privacyMode: str(payload.privacy_mode),
    downloadBlocked: bool(payload.download_blocked),
    restartRequired: bool(payload.restart_required),
  };
}

/** 0..1 once the download has announced its size, `null` before. */
export function voiceModelFraction(progress: VoiceModelProgress | null): number | null {
  if (!progress || progress.total <= 0) return null;
  return Math.min(1, progress.done / progress.total);
}

/** Whether "Herunterladen" can do something: files missing, nothing running, downloads allowed. */
export function canDownloadVoiceModel(status: VoiceModelStatus): boolean {
  return !status.running && !status.present && status.files > 0 && !status.downloadBlocked;
}
