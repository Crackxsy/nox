/**
 * The security PIN, resuming from safe mode and relaxing privacy - the parts of the Status and
 * Settings pages that have to be exactly right, as pure functions.
 *
 * Refusals are read from the error frame's `details.reason` (`src/nox/security/pin_setup.py`), a
 * stable word, never from the English message: `pinRefusal` is the only place that looks at it.
 * The PIN itself never enters this module; nothing here stores, logs or compares one.
 */

import type {
  PinStatus as WirePinStatus,
  SecurityResumeResult as WireResumeResult,
} from '../../../shared/generated/ipc';
import { isRecord, numOrNull, str, strOrNull } from '../../../shared/guards';
import { IpcError } from '../../../shared/ipc';
import type { KillPhase } from './chat';
import type { FieldMap } from './wire';

// ---- PIN status ------------------------------------------------------------------------------

export type PinEntryState = 'not_set' | 'valid' | 'invalid' | 'unavailable';

export interface PinState {
  state: PinEntryState;
  /** A PIN-gated request has to carry a PIN (also true when the entry is broken or unreadable). */
  configured: boolean;
  minLength: number;
  /** Relaxing privacy needs the PIN right now. */
  gateRequired: boolean;
  /** Leaving the current safe mode needs the PIN (a security-path kill while a PIN is set). */
  resumeRequiresPin: boolean;
  /** Set while failed attempts lock the PIN. */
  lockedUntil: string | null;
}

/** See `model/wire.ts`. */
export const PIN_STATUS_FIELDS: FieldMap<WirePinStatus, PinState> = {
  state: 'state',
  configured: 'configured',
  min_length: 'minLength',
  gate_required: 'gateRequired',
  resume_requires_pin: 'resumeRequiresPin',
  locked_until: 'lockedUntil',
};

const ENTRY_STATES: readonly PinEntryState[] = ['not_set', 'valid', 'invalid', 'unavailable'];

/** Default minimum when an older core does not say; the core enforces its own anyway. */
export const DEFAULT_PIN_MIN_LENGTH = 6;

/** `security.pin.status {}`. An older core that sends only `{configured}` still parses. */
export function parsePinStatus(payload: unknown): PinState | null {
  if (!isRecord(payload) || typeof payload.configured !== 'boolean') return null;
  const state = str(payload.state);
  const known = (ENTRY_STATES as readonly string[]).includes(state);
  return {
    state: known ? (state as PinEntryState) : payload.configured ? 'valid' : 'not_set',
    configured: payload.configured,
    minLength: numOrNull(payload.min_length) ?? DEFAULT_PIN_MIN_LENGTH,
    gateRequired: payload.gate_required === true,
    resumeRequiresPin: payload.resume_requires_pin === true,
    lockedUntil: strOrNull(payload.locked_until),
  };
}

// ---- refusals --------------------------------------------------------------------------------

export type PinRefusalReason =
  | 'pin_required'
  | 'pin_wrong'
  | 'locked'
  | 'too_short'
  | 'too_long'
  | 'not_set'
  | 'invalid_entry'
  | 'store_unavailable'
  | 'backend_missing';

const REFUSAL_REASONS: readonly PinRefusalReason[] = [
  'pin_required',
  'pin_wrong',
  'locked',
  'too_short',
  'too_long',
  'not_set',
  'invalid_entry',
  'store_unavailable',
  'backend_missing',
];

export interface PinRefusal {
  reason: PinRefusalReason;
  remainingAttempts: number | null;
  lockedUntil: string | null;
  minLength: number | null;
}

function refusalFrom(record: Record<string, unknown>): PinRefusal | null {
  const reason = str(record.reason);
  if (!(REFUSAL_REASONS as readonly string[]).includes(reason)) return null;
  return {
    reason: reason as PinRefusalReason,
    remainingAttempts: numOrNull(record.remaining_attempts),
    lockedUntil: strOrNull(record.locked_until),
    minLength: numOrNull(record.min_length),
  };
}

/**
 * A thrown request error that is about the PIN, or null for anything else (which the caller then
 * shows with the core's own wording).
 */
export function pinRefusal(e: unknown): PinRefusal | null {
  if (!(e instanceof IpcError)) return null;
  return refusalFrom(e.details);
}

// ---- resume ----------------------------------------------------------------------------------

export interface ResumeResult {
  ok: boolean;
  refusal: PinRefusal | null;
  /** `rearmed`, `not_in_safe_mode`, `unreachable`, `standalone`, or '' from an older core. */
  supervisor: string;
}

/** See `model/wire.ts`. */
export const RESUME_FIELDS: FieldMap<WireResumeResult, ResumeResult> = {
  ok: 'ok',
  reason: 'refusal',
  remaining_attempts: 'refusal',
  locked_until: 'refusal',
  supervisor: 'supervisor',
};

/** `security.resume` answer. A refusal without a known reason is still a refusal. */
export function parseResumeResult(payload: unknown): ResumeResult {
  const rec = isRecord(payload) ? payload : {};
  return {
    ok: rec.ok === true,
    refusal: rec.ok === true ? null : refusalFrom(rec),
    supervisor: str(rec.supervisor),
  };
}

// ---- kill switch after a resume --------------------------------------------------------------

/**
 * The kill button is usable again once the core has left safe mode - and only then.
 *
 * `sent` stays terminal while the kill is in flight or in force, which is what keeps a double
 * click from firing twice. What ends it is the core reporting a level other than `safe_mode`
 * *after* having reported `safe_mode`: a resume from this page, the tray or the shell. A level
 * that was never `safe_mode` (the kill request still travelling) does not re-arm anything.
 */
export function killAfterLevel(
  phase: KillPhase,
  previousLevel: string | null,
  level: string | null,
): KillPhase {
  if (phase !== 'sent') return phase;
  if (previousLevel === 'safe_mode' && level !== null && level !== 'safe_mode') return 'idle';
  return phase;
}

// ---- privacy ---------------------------------------------------------------------------------

/** Most to least protective; a move toward the end relaxes (`nox.security.gate`). */
const PRIVACY_ORDER = ['offline', 'private', 'balanced', 'full'] as const;

/** Whether `target` gives Nox more freedom than `current` - the changes the PIN gate guards. */
export function relaxesPrivacy(current: string | null, target: string): boolean {
  const from = PRIVACY_ORDER.indexOf(current as (typeof PRIVACY_ORDER)[number]);
  const to = PRIVACY_ORDER.indexOf(target as (typeof PRIVACY_ORDER)[number]);
  if (to < 0) return false;
  // An unknown current mode counts as the strictest: fail closed, ask for the PIN.
  return to > (from < 0 ? 0 : from);
}

export interface PrivacySetResult {
  mode: string | null;
  applied: boolean;
  requiresConfirmation: boolean;
}

/** `privacy.set` answer: the state dump plus `applied` / `requires_confirmation`. */
export function parsePrivacySetResult(payload: unknown): PrivacySetResult {
  const rec = isRecord(payload) ? payload : {};
  return {
    mode: strOrNull(rec.mode),
    // An older core does not say; then the mode itself is the evidence.
    applied: rec.applied === undefined ? true : rec.applied === true,
    requiresConfirmation: rec.requires_confirmation === true,
  };
}
