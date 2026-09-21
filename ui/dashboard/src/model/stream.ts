/**
 * Stream Bot view model (Spec v0.2, EPIC-11): session status, the live Twitch chat feed and the
 * Funken leaderboard.
 *
 * The chat feed is bounded and lives only for this connection — the core owns the durable
 * `chat_events` table, and this list never pretends to be it.
 */

import type { FunkenTopEntry as WireFunkenTopEntry } from '../../../shared/generated/ipc';
import { bool, isRecord, numOrNull, str, strOrNull } from '../../../shared/guards';
import type { FieldMap } from './wire';

export interface StreamPluginStatus {
  obs: string;
  twitch: string;
}

/** `stream.session.status {}` response body (`nox.ipc.protocol.StreamSessionStatus`). */
export interface StreamSessionStatusState {
  active: boolean;
  sessionId: string | null;
  startedAt: string | null;
  scene: string | null;
  plugins: StreamPluginStatus;
}

export const INITIAL_PLUGIN_STATUS: StreamPluginStatus = { obs: 'unknown', twitch: 'unknown' };
export const INITIAL_SESSION_STATUS: StreamSessionStatusState = {
  active: false,
  sessionId: null,
  startedAt: null,
  scene: null,
  plugins: INITIAL_PLUGIN_STATUS,
};

/** One live `twitch.chat_message` event, kept only for this session's feed (not the log). */
export interface StreamChatEntry {
  id: string;
  viewerId: string;
  text: string;
  addressed: boolean;
  relevance: number;
  ts: string;
}

/** `stream.funken.top {limit}` response entry (`nox.ipc.protocol.FunkenTopEntry`). */
export interface FunkenTopEntry {
  viewerId: string;
  displayName: string;
  balance: number;
  tier: string;
}

/** See `model/wire.ts`. */
export const FUNKEN_FIELDS: FieldMap<WireFunkenTopEntry, FunkenTopEntry> = {
  viewer_id: 'viewerId',
  display_name: 'displayName',
  balance: 'balance',
  tier: 'tier',
};

export interface StreamState {
  session: StreamSessionStatusState;
  chat: StreamChatEntry[];
  funkenTop: FunkenTopEntry[];
  /** Why the stream plugins are not loaded, as the core worded it; '' while nothing said so. */
  pluginReason: string;
}

export const INITIAL_STREAM_STATE: StreamState = {
  session: INITIAL_SESSION_STATUS,
  chat: [],
  funkenTop: [],
  pluginReason: '',
};

/** Keep the live chat feed bounded; the core owns the durable `chat_events` table. */
export const STREAM_CHAT_LIMIT = 200;

/** `stream.session.status {}` response: `nox.ipc.protocol.StreamSessionStatus`. */
export function parseStreamSessionStatus(payload: unknown): StreamSessionStatusState | null {
  if (!isRecord(payload)) return null;
  const plugins = isRecord(payload.plugins) ? payload.plugins : {};
  return {
    active: bool(payload.active),
    sessionId: strOrNull(payload.session_id),
    startedAt: strOrNull(payload.started_at),
    scene: strOrNull(payload.scene),
    plugins: {
      obs: str(plugins.obs, 'unknown'),
      twitch: str(plugins.twitch, 'unknown'),
    },
  };
}

/** `stream.funken.top {limit}` response: `nox.ipc.protocol.FunkenTop`. */
export function parseFunkenTop(payload: unknown): FunkenTopEntry[] | null {
  if (!isRecord(payload) || !Array.isArray(payload.viewers)) return null;
  const out: FunkenTopEntry[] = [];
  for (const entry of payload.viewers) {
    if (!isRecord(entry)) continue;
    const viewerId = str(entry.viewer_id);
    if (!viewerId) continue;
    out.push({
      viewerId,
      displayName: str(entry.display_name),
      balance: numOrNull(entry.balance) ?? 0,
      tier: str(entry.tier, 'none'),
    });
  }
  return out;
}

/**
 * One `twitch.chat_message` event. The core's own `chat_event_id` is the id when it sent one; the
 * monotonic `seq` from `DashboardState` is the fallback, because the previous key (timestamp plus
 * list length) stopped being unique the moment the feed hit `STREAM_CHAT_LIMIT`.
 */
export function chatEntry(
  payload: Record<string, unknown>,
  ts: string,
  seq: number,
): StreamChatEntry | null {
  const text = str(payload.text);
  if (!text) return null;
  const eventId = numOrNull(payload.chat_event_id);
  return {
    id: eventId === null ? `chat-${seq}` : `chat-e${eventId}`,
    viewerId: str(payload.viewer_id),
    text,
    addressed: bool(payload.addressed_to_nox),
    relevance: numOrNull(payload.relevance) ?? 0,
    ts,
  };
}

/**
 * A name for the person who wrote a chat message. `twitch.chat_message` carries only `viewer_id`,
 * so the Funken leaderboard — the one place the core does send display names — is consulted first.
 * When it does not know the viewer, the id is shown as-is; no name is ever invented.
 */
export function viewerName(entry: StreamChatEntry, funkenTop: readonly FunkenTopEntry[]): string {
  const known = funkenTop.find((v) => v.viewerId === entry.viewerId);
  return known?.displayName || entry.viewerId;
}
