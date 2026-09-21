/**
 * Chat turns and the kill-switch confirm machine — the two pieces of screen logic that are pure
 * enough to live in the model and important enough to be tested there.
 */

import type { ChatSendResult, ChatStreamFrame } from '../../../shared/generated/ipc';

export interface ChatMessage {
  id: string;
  role: 'user' | 'nox';
  text: string;
  /** true while stream frames are still arriving. */
  streaming: boolean;
  provider: string | null;
  degraded: boolean;
  error: string | null;
}

/**
 * Text delta of one `chat.send` stream frame: `ChatStreamFrame {delta, done}`
 * (src/nox/ipc/protocol.py, generated at ui/shared/generated/ipc.ts). The hub validates every
 * stream frame against that shape, so there is no `chunk`/`text` fallback to read here.
 */
export function streamDelta(payload: Record<string, unknown>): string {
  const delta = (payload as Partial<ChatStreamFrame>).delta;
  return typeof delta === 'string' ? delta : '';
}

/**
 * Final `chat.send` response: `ChatSendResult {request_id, text, provider, degraded}`. Returns
 * `text: null` when the payload is not a usable answer instead of inventing one.
 */
export function finalAnswer(payload: Record<string, unknown>): {
  text: string | null;
  provider: string | null;
  degraded: boolean;
} {
  const result = payload as Partial<ChatSendResult>;
  return {
    text: typeof result.text === 'string' ? result.text : null,
    provider: typeof result.provider === 'string' ? result.provider : null,
    degraded: result.degraded === true,
  };
}

// ---- kill switch confirm -------------------------------------------------------------------

export type KillPhase = 'idle' | 'confirm' | 'sent';

/**
 * Two-step kill switch. Three properties matter and all three are tested:
 *  - one stray click can never fire it (`idle` → `confirm`, not `sent`);
 *  - the armed step expires, so a forgotten armed button stops being dangerous;
 *  - `sent` is terminal. Pressing the button again after a kill must *not* fire a second one, and
 *    the old table returned `sent` for that press, which the page then read as "fire now" — a
 *    one-click kill switch with no confirm step. `sent` now stays `sent` until the page reloads.
 */
export function killNext(phase: KillPhase, action: 'press' | 'cancel' | 'timeout'): KillPhase {
  if (phase === 'sent') return 'sent';
  if (action === 'cancel' || action === 'timeout') return 'idle';
  return phase === 'idle' ? 'confirm' : 'sent';
}

export const KILL_CONFIRM_MS = 6000;

/** How long `chat.send` may stay silent before the client gives up (idle, not total — see ipc.ts). */
export const CHAT_IDLE_TIMEOUT_MS = 60000;
