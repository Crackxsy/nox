/**
 * One place that turns a thrown value into a line a person can read.
 *
 * The rule from the code standards is "users never see tracebacks, every user-facing error says
 * what happened": that needs a translated sentence for the cases the UI recognises and, for
 * everything else, the core's own message behind a prefix — honest, never swallowed, never faked.
 *
 * `reasonText` is the raw half of the same job: the untranslated string a caller may put in a
 * muted secondary line under a translated one, so a reason this UI does not know yet is still
 * visible instead of being dropped.
 */

import { IpcError } from './ipc';

/** How a request failed, as far as the UI is allowed to care. */
export type FailureKind =
  /** The core does not know this request at all — the feature is not installed. */
  | 'unknown_request'
  /** The core knows it but this role/profile may not call it. */
  | 'refused'
  /** The subsystem is switched off or not reachable right now. */
  | 'unavailable'
  /** No answer within the request timeout. */
  | 'timeout'
  /** Anything else, including non-IPC errors. */
  | 'failed';

export function failureKind(e: unknown): FailureKind {
  if (!(e instanceof IpcError)) return 'failed';
  switch (e.code) {
    case 'not_found':
      return 'unknown_request';
    case 'permission.denied':
      return 'refused';
    case 'unavailable':
      return 'unavailable';
    case 'timeout':
      return 'timeout';
    default:
      return 'failed';
  }
}

/** The core's own wording, or the JS error message, or `''`. Never a stack trace. */
export function reasonText(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === 'string') return e;
  return '';
}

/** `"<prefix>: <reason>"`, e.g. `"Fehler: PIN wird benötigt"`. */
export function errorText(prefix: string, e: unknown): string {
  const reason = reasonText(e);
  return reason ? `${prefix}: ${reason}` : prefix;
}
