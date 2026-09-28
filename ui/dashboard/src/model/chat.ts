/**
 * Chat turns, the persisted history and the kill-switch confirm machine — the pieces of screen
 * logic that are pure enough to live in the model and important enough to be tested there.
 */

import type {
  ChatHistoryTurn,
  ChatSendResult,
  ChatStreamFrame,
} from '../../../shared/generated/ipc';
import { isRecord } from '../../../shared/guards';

export interface ChatMessage {
  id: string;
  role: 'user' | 'nox';
  text: string;
  /** true while stream frames are still arriving. */
  streaming: boolean;
  provider: string | null;
  degraded: boolean;
  error: string | null;
  /** When a persisted turn was said; only history turns carry it. */
  at?: string;
}

// ---- persisted history ---------------------------------------------------------------------

export interface ChatHistoryPage {
  /** Oldest first, as the core sends them. */
  messages: ChatMessage[];
  /** Older turns exist; ask again with `before` = the first turn's id. */
  hasMore: boolean;
  /** The id of the oldest turn in this page, the cursor for the next one. */
  oldestId: number | null;
}

function historyMessage(turn: ChatHistoryTurn): ChatMessage {
  return {
    id: `h-${turn.id}`,
    role: turn.role === 'user' ? 'user' : 'nox',
    text: turn.text,
    streaming: false,
    provider: turn.provider ? turn.provider : null,
    degraded: false,
    error: null,
    at: turn.ts,
  };
}

function isHistoryTurn(x: unknown): x is ChatHistoryTurn {
  return (
    isRecord(x) &&
    typeof x.id === 'number' &&
    typeof x.ts === 'string' &&
    typeof x.role === 'string' &&
    typeof x.text === 'string'
  );
}

/**
 * `chat.history` answer. `notBefore` drops turns said at or after that moment: the first page is
 * asked for when the page opens, and a turn typed in this window already shows as a live message.
 * Returns null when the payload is not a history at all, so the page can say it failed.
 */
export function parseChatHistory(payload: unknown, notBefore: string | null = null): ChatHistoryPage | null {
  if (!isRecord(payload) || !Array.isArray(payload.turns)) return null;
  const turns = payload.turns.filter(isHistoryTurn);
  const cutoff = notBefore === null ? null : Date.parse(notBefore);
  const shown =
    cutoff === null || Number.isNaN(cutoff)
      ? turns
      : turns.filter((turn) => Date.parse(turn.ts) < cutoff);
  return {
    messages: shown.map(historyMessage),
    hasMore: payload.has_more === true,
    oldestId: turns.length > 0 ? turns[0].id : null,
  };
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
 *  - `sent` is terminal for presses. Pressing the button again after a kill must *not* fire a
 *    second one, and the old table returned `sent` for that press, which the page then read as
 *    "fire now" — a one-click kill switch with no confirm step. Only leaving safe mode ends it
 *    (`killAfterLevel` in `security.ts`), so the button is usable again after a resume.
 */
export function killNext(phase: KillPhase, action: 'press' | 'cancel' | 'timeout'): KillPhase {
  if (phase === 'sent') return 'sent';
  if (action === 'cancel' || action === 'timeout') return 'idle';
  return phase === 'idle' ? 'confirm' : 'sent';
}

export const KILL_CONFIRM_MS = 6000;

/** How many persisted turns one `chat.history` page asks for. */
export const CHAT_HISTORY_PAGE = 30;

/** How long `chat.send` may stay silent before the client gives up (idle, not total — see ipc.ts). */
export const CHAT_IDLE_TIMEOUT_MS = 60000;
