/** Envelope v1 (src/nox/ipc/protocol.py) — typed, versioned, identifiable, validated on receive. */

import { isRecord } from './guards';

export const SCHEMA_VERSION = 1;

export type Kind = 'event' | 'request' | 'response' | 'error' | 'stream';
export type Role = 'core' | 'shell' | 'worker' | 'plugin' | 'pet' | 'dashboard' | 'supervisor' | 'remote';

export interface Source {
  role: Role;
  id: string;
}

export interface Envelope<P = Record<string, unknown>> {
  v: number;
  id: string;
  ts: string;
  kind: Kind;
  name: string;
  corr: string | null;
  src: Source;
  payload: P;
}

export interface ErrorPayload {
  code: string;
  message: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
}

const KINDS: ReadonlySet<string> = new Set(['event', 'request', 'response', 'error', 'stream']);
const ROLES: ReadonlySet<string> = new Set([
  'core', 'shell', 'worker', 'plugin', 'pet', 'dashboard', 'supervisor', 'remote',
]);
const NAME_RE = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/;

export function uuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // Fallback for very old runtimes; not cryptographically strong but only used as a message id.
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export function makeEnvelope(
  kind: Kind,
  name: string,
  src: Source,
  payload: Record<string, unknown> = {},
  corr: string | null = null,
): Envelope {
  return { v: SCHEMA_VERSION, id: uuid(), ts: new Date().toISOString(), kind, name, corr, src, payload };
}

/** Plausible range for a numeric `ts`, in epoch **seconds**: 2001-09-09 to 2286-11-20. */
const TS_SECONDS_MIN = 1_000_000_000;
const TS_SECONDS_MAX = 9_999_999_999;

function normaliseTs(ts: unknown): string {
  if (typeof ts === 'string') return ts;
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return '';
  // A millisecond timestamp would land in the year 57000; refuse it instead of rendering a date
  // nobody can act on.
  if (ts < TS_SECONDS_MIN || ts > TS_SECONDS_MAX) return '';
  return new Date(ts * 1000).toISOString();
}

/**
 * Parse one text frame. Returns null for anything that is not a well-formed v1 envelope —
 * the caller logs and drops it; nothing is "processed somehow" (IPC Model).
 *
 * `ts` is normally an ISO-8601 string; a numeric one is read as epoch **seconds** (see
 * `normaliseTs`), which is the only numeric form `nox.ipc.protocol` ever produces.
 */
export function parseEnvelope(text: string): Envelope | null {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isRecord(raw)) return null;
  const { v, id, ts, kind, name, corr, src, payload } = raw;
  if (v !== SCHEMA_VERSION) return null;
  if (typeof id !== 'string' || id.length === 0) return null;
  if (typeof kind !== 'string' || !KINDS.has(kind)) return null;
  if (typeof name !== 'string' || name.length > 96 || !NAME_RE.test(name)) return null;
  if (corr !== undefined && corr !== null && typeof corr !== 'string') return null;
  if (!isRecord(src) || typeof src.role !== 'string' || !ROLES.has(src.role)) return null;
  if (typeof src.id !== 'string' || src.id.length === 0) return null;
  if (payload !== undefined && !isRecord(payload)) return null;
  return {
    v: SCHEMA_VERSION,
    id,
    ts: normaliseTs(ts),
    kind: kind as Kind,
    name,
    corr: typeof corr === 'string' ? corr : null,
    src: { role: src.role as Role, id: src.id },
    payload: (payload ?? {}) as Record<string, unknown>,
  };
}

/** Glob match for subscription patterns: "pet.*" matches "pet.state_changed", "*" matches all. */
export function matchesPattern(name: string, pattern: string): boolean {
  if (pattern === '*') return true;
  if (pattern.endsWith('.*')) return name.startsWith(pattern.slice(0, -1));
  return name === pattern;
}
