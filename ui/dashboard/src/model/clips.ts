/**
 * Clip Pipeline view model (Spec v0.6, EPIC-15).
 *
 * Two kinds of row live here and they are deliberately different types. A `ClipRecord` is a full
 * row the core read out of the `clips` table. A `PendingClip` is what a `clip.saved` *event*
 * carries — an id, a file, a length, and nothing else. The reducer used to promote the second into
 * the first by filling `origin_event_id`, `session_id` and `checksum` with empty strings, i.e. by
 * inventing exactly the fields `parseClipRecord` refuses to invent. It no longer does.
 */

import type {
  ClipExportResult as WireClipExportResult,
  ClipRecord as WireClipRecord,
} from '../../../shared/generated/ipc';
import { isRecord, numOrNull, str, strOrNull, stringList } from '../../../shared/guards';
import type { FieldMap } from './wire';

/** One `clips` row (`nox.ipc.protocol.ClipRecord`). */
export interface ClipRecord {
  id: string;
  /** event | manual | marker_promoted */
  source: string;
  triggerKind: string;
  originEventId: string;
  sessionId: string;
  filePath: string;
  durationS: number;
  createdAt: string;
  thumbnailPath: string | null;
  tags: string[];
  /** new | reviewed | exported | discarded */
  status: string;
  parentClipId: string | null;
  checksum: string;
  notes: string;
}

/** See `model/wire.ts`. */
export const CLIP_FIELDS: FieldMap<WireClipRecord, ClipRecord> = {
  id: 'id',
  source: 'source',
  trigger_kind: 'triggerKind',
  origin_event_id: 'originEventId',
  session_id: 'sessionId',
  file_path: 'filePath',
  duration_s: 'durationS',
  created_at: 'createdAt',
  thumbnail_path: 'thumbnailPath',
  tags: 'tags',
  status: 'status',
  parent_clip_id: 'parentClipId',
  checksum: 'checksum',
  notes: 'notes',
};

/** What a `clip.saved` event knows — strictly less than a `ClipRecord`, and typed that way. */
export interface PendingClip {
  id: string;
  source: string;
  triggerKind: string;
  filePath: string;
  durationS: number;
  createdAt: string;
  tags: string[];
}

export interface ClipsState {
  items: ClipRecord[];
  /** Clips announced by an event but not yet read back through `clip.list`. */
  pending: PendingClip[];
}

export const INITIAL_CLIPS_STATE: ClipsState = { items: [], pending: [] };

/**
 * One entry of a `clip.list {}` response. `null` when the entry is missing its required fields,
 * rather than inventing placeholders.
 */
export function parseClipRecord(entry: unknown): ClipRecord | null {
  if (!isRecord(entry)) return null;
  const id = str(entry.id);
  if (!id) return null;
  return {
    id,
    source: str(entry.source),
    triggerKind: str(entry.trigger_kind),
    originEventId: str(entry.origin_event_id),
    sessionId: str(entry.session_id),
    filePath: str(entry.file_path),
    durationS: numOrNull(entry.duration_s) ?? 0,
    createdAt: str(entry.created_at),
    thumbnailPath: strOrNull(entry.thumbnail_path),
    tags: stringList(entry.tags),
    status: str(entry.status, 'new'),
    parentClipId: strOrNull(entry.parent_clip_id),
    checksum: str(entry.checksum),
    notes: str(entry.notes),
  };
}

/** `clip.list {status?, limit?}` response: `nox.ipc.protocol.ClipListResult`. */
export function parseClipList(payload: unknown): ClipRecord[] | null {
  if (!isRecord(payload) || !Array.isArray(payload.clips)) return null;
  return payload.clips.map(parseClipRecord).filter((c): c is ClipRecord => c !== null);
}

/** `clip.tag {…}` response: `nox.ipc.protocol.ClipTagResult` — one record under `clip`. */
export function parseClipTagResult(payload: unknown): ClipRecord | null {
  return isRecord(payload) ? parseClipRecord(payload.clip) : null;
}

export interface ClipExportResult {
  ok: boolean;
  /** Absolute path, or `null` — the contract marks it optional, so it is never assumed present. */
  exportPath: string | null;
  /** The core's reason for a refusal; `''` when it gave none. */
  reason: string;
}

/** See `model/wire.ts`. */
export const CLIP_EXPORT_FIELDS: FieldMap<WireClipExportResult, ClipExportResult> = {
  ok: 'ok',
  export_path: 'exportPath',
  reason: 'reason',
};

/** `clip.export {clip_id}` response. `null` when the payload is not a result at all. */
export function parseClipExportResult(payload: unknown): ClipExportResult | null {
  if (!isRecord(payload) || typeof payload.ok !== 'boolean') return null;
  return {
    ok: payload.ok,
    exportPath: strOrNull(payload.export_path),
    reason: str(payload.reason),
  };
}

export function applyClipList(state: ClipsState, payload: unknown): ClipsState {
  const items = parseClipList(payload);
  if (!items) return state;
  // Everything the list knows about is no longer merely "announced".
  const known = new Set(items.map((c) => c.id));
  return { items, pending: state.pending.filter((p) => !known.has(p.id)) };
}

/** Upsert one full record (a `clip.tag` answer, or a re-read row). */
export function applyClipRecord(state: ClipsState, clip: ClipRecord): ClipsState {
  const items = state.items.some((c) => c.id === clip.id)
    ? state.items.map((c) => (c.id === clip.id ? clip : c))
    : [clip, ...state.items];
  return { items, pending: state.pending.filter((p) => p.id !== clip.id) };
}

/** A `clip.saved` event: remembered as a pending row, never faked into a full record. */
export function applyClipSaved(
  state: ClipsState,
  payload: Record<string, unknown>,
  ts: string,
): ClipsState {
  const id = str(payload.clip_id);
  if (!id || state.items.some((c) => c.id === id) || state.pending.some((p) => p.id === id)) {
    return state;
  }
  const entry: PendingClip = {
    id,
    source: str(payload.source),
    triggerKind: str(payload.trigger_kind),
    filePath: str(payload.file_path),
    durationS: numOrNull(payload.duration_s) ?? 0,
    createdAt: ts,
    tags: stringList(payload.tags),
  };
  return { ...state, pending: [entry, ...state.pending] };
}

/** A `clip.exported` event: only a clip we already have a full record for can change status. */
export function applyClipExported(state: ClipsState, clipId: string): ClipsState {
  const existing = state.items.find((c) => c.id === clipId);
  if (!existing) return state;
  return applyClipRecord(state, { ...existing, status: 'exported' });
}
