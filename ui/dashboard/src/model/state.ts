/**
 * The dashboard's whole state, and the two facts that are true only while connected.
 *
 * Honesty rules the rest of the model follows (ENGINEERING.md "no fake implementations"):
 * - nothing is invented; a value the core never sent stays `null` and the UI shows "unbekannt";
 * - on disconnect, live-only facts (capture flags, plugin connections) are cleared rather than
 *   kept on screen as if they were still current.
 */

import type { ClipsState } from './clips';
import { INITIAL_CLIPS_STATE } from './clips';
import type { HealthState } from './health';
import type { ProactiveNotification } from './notifications';
import type { Provider } from './providers';
import type { StreamState } from './stream';
import { INITIAL_PLUGIN_STATUS, INITIAL_STREAM_STATE } from './stream';

export interface VoiceInfo {
  sttEngine: string;
  ttsEngine: string;
  listening: boolean;
  speaking: boolean;
  routing: string;
  lastLatencyMs: number | null;
}

export interface CaptureFlags {
  microphone: boolean;
  camera: boolean;
  screen: boolean;
  cloud: boolean;
}

export interface AuditRow {
  seq: number;
  actor: string;
  tool: string;
  action: string;
  target: string;
  decision: string;
  result: string;
  taskId: string | null;
  ts: string;
}

export interface DashboardState {
  connected: boolean;
  health: HealthState | null;
  providers: Provider[] | null;
  voice: VoiceInfo | null;
  privacyMode: string | null;
  capture: CaptureFlags | null;
  mode: string | null;
  muted: boolean | null;
  systemLevel: string | null;
  audit: AuditRow[];
  stream: StreamState;
  notifications: ProactiveNotification[];
  clips: ClipsState;
  /** Bumped by every `settings.changed` event; the Settings page reloads when it moves. */
  settingsRevision: number;
  /** Last `twitch.auth.changed` state, or null while none was received on this connection. */
  twitchAuth: string | null;
  /**
   * Monotonic counter for rows this app has to give a React key to (toasts, live chat messages).
   * It lives in the state rather than in a module variable so the reducer stays pure and two tests
   * cannot leak ids into each other.
   */
  seq: number;
}

export const INITIAL_STATE: DashboardState = {
  connected: false,
  health: null,
  providers: null,
  voice: null,
  privacyMode: null,
  capture: null,
  mode: null,
  muted: null,
  systemLevel: null,
  audit: [],
  stream: INITIAL_STREAM_STATE,
  notifications: [],
  clips: INITIAL_CLIPS_STATE,
  settingsRevision: 0,
  twitchAuth: null,
  seq: 0,
};

export const PRIVACY_MODES = ['full', 'balanced', 'private', 'offline'] as const;
export type PrivacyMode = (typeof PRIVACY_MODES)[number];

/** `nox.core.state.Mode`, in the order the select offers them. */
export const MODES = [
  'companion', 'coding', 'project', 'stream', 'rocket_league', 'creative', 'research', 'focus', 'idle',
] as const;

/** Keep the live audit list bounded; the core owns the complete append-only log. */
export const AUDIT_LIMIT = 500;

/** Connection changes. Offline clears facts that are only true "right now". */
export function setConnected(state: DashboardState, connected: boolean): DashboardState {
  if (connected === state.connected) return state;
  if (connected) return { ...state, connected };
  // Plugin connection state is only true "right now"; the chat feed and Funken leaderboard stay
  // as the last known snapshot, same treatment as `capture`.
  return {
    ...state,
    connected,
    capture: null,
    stream: { ...state.stream, session: { ...state.stream.session, plugins: INITIAL_PLUGIN_STATUS } },
  };
}
