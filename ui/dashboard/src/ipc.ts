/**
 * Dashboard-role IPC client. Role `dashboard` may send read requests plus mode.set, privacy.set,
 * security.*, voice.mute and chat.send (IPC Model §Security rules) — this module exposes exactly
 * those and nothing else, so the UI cannot accidentally try a request the hub will reject.
 */

import type { Envelope } from '../../shared/envelope';
import { type ConnStatus, IpcClient, IpcError, resolveWsUrl } from '../../shared/ipc';
import { CHAT_IDLE_TIMEOUT_MS } from './model/chat';

export type { Envelope, ConnStatus };
export { IpcClient, IpcError };

export const DASHBOARD_PATTERNS = [
  'system.*',
  'state.changed',
  'health.*',
  'privacy.*',
  'security.*',
  'voice.*',
  'tts.*',
  'ai.*',
  'pet.*',
  'stream.*',
  'obs.*',
  'twitch.*',
  'proactive.*',
  'clip.*',
  // `plugin.failed` carries the reason a stream plugin did not load (usually the active security
  // profile). It is emitted during boot, i.e. normally before this page connects, so the Stream
  // tab only shows it for a failure that happens *during* the session — a profile switch, say.
  // See the report: a `plugin.status` read request would let the page ask at connect time.
  'plugin.*',
  // Settings page: `settings.changed {paths}` and `twitch.auth.changed {state}` (the latter is
  // already covered by `twitch.*`, listed here only so the pairing is obvious).
  'settings.*',
];

export interface DashboardIpcHandlers {
  onEvent: (env: Envelope) => void;
  onStatus: (status: ConnStatus, detail?: string) => void;
}

export async function createDashboardClient(
  token: string,
  handlers: DashboardIpcHandlers,
): Promise<IpcClient> {
  const url = await resolveWsUrl(window.location);
  const client = new IpcClient({
    url,
    token,
    role: 'dashboard',
    id: `dashboard:${Math.random().toString(36).slice(2, 8)}`,
    patterns: DASHBOARD_PATTERNS,
    clientVersion: '0.1.0',
    onEvent: handlers.onEvent,
    onStatus: handlers.onStatus,
  });
  client.connect();
  return client;
}

export const api = {
  stateGet: (c: IpcClient) => c.request('state.get', {}),
  healthGet: (c: IpcClient) => c.request('health.get', {}),
  providers: (c: IpcClient) => c.request('ai.providers', {}),
  setPrivacy: (c: IpcClient, mode: string) => c.request('privacy.set', { mode }),
  setMode: (c: IpcClient, mode: string) => c.request('mode.set', { mode }),
  setMuted: (c: IpcClient, muted: boolean) => c.request('voice.mute', { muted }),
  kill: (c: IpcClient, reason: string) => c.request('security.kill', { reason, origin: 'ui' }),
  // A model on a cold cache can take well past the 10 s default before the first token; the
  // timeout is an idle timeout (`shared/ipc.ts`), so every arriving frame restarts it.
  chat: (c: IpcClient, text: string, onChunk: (env: Envelope) => void) =>
    c.request('chat.send', { text }, onChunk, CHAT_IDLE_TIMEOUT_MS),
  panic: (c: IpcClient) => c.request('security.panic', { origin: 'ui' }),
  streamStatus: (c: IpcClient) => c.request('stream.session.status', {}),
  funkenTop: (c: IpcClient, limit = 10) => c.request('stream.funken.top', { limit }),
  remoteDevices: (c: IpcClient) => c.request('remote.devices.list', {}),
  remotePairStart: (c: IpcClient, name: string) => c.request('remote.pair.start', { name }),
  remoteUnpair: (c: IpcClient, deviceId: string) =>
    c.request('remote.unpair', { device_id: deviceId }),
  healthHistory: (c: IpcClient, limit = 50, component = '') =>
    c.request('health.history', { limit, component }),
  clipList: (c: IpcClient, status: string | null = null, limit = 50) =>
    c.request('clip.list', { status, limit }),
  clipTag: (c: IpcClient, clipId: string, tags: string[] | null, notes: string | null = null) =>
    c.request('clip.tag', { clip_id: clipId, tags, notes }),
  clipExport: (c: IpcClient, clipId: string) => c.request('clip.export', { clip_id: clipId }),

  // -- editable settings (Settings page) ---------------------------------------------------------
  configGet: (c: IpcClient) => c.request('config.get', {}),
  configSet: (c: IpcClient, values: Record<string, unknown>) => c.request('config.set', { values }),
  secretsStatus: (c: IpcClient) => c.request('secrets.status', {}),
  /** `{configured}` — whether a secret change has to carry a PIN (#23). */
  pinStatus: (c: IpcClient) => c.request('security.pin.status', {}),
  // The PIN is only ever a request field: it is passed in per call, sent when there is one, and
  // never stored anywhere in this UI. An empty string is no PIN at all, so it is not sent —
  // that way the core answers "PIN required" instead of "PIN wrong".
  secretSet: (c: IpcClient, name: string, value: string, pin = '') =>
    c.request('secrets.set', pin ? { name, value, pin } : { name, value }),
  secretDelete: (c: IpcClient, name: string, pin = '') =>
    c.request('secrets.delete', pin ? { name, pin } : { name }),
  twitchAuthStart: (c: IpcClient) => c.request('twitch.auth.start', {}),
  twitchAuthStatus: (c: IpcClient) => c.request('twitch.auth.status', {}),
  twitchAuthDisconnect: (c: IpcClient) => c.request('twitch.auth.disconnect', {}),
  personalityGet: (c: IpcClient) => c.request('personality.get', {}),
  personalitySet: (c: IpcClient, text: string) => c.request('personality.set', { text }),
};
