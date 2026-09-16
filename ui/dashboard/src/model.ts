/**
 * Dashboard view model: pure parsing + reduction of IPC payloads (Envelope v1 payloads defined in
 * src/nox/core/events.py and src/nox/core/state.py). No React, no side effects — everything here is
 * unit-tested.
 *
 * Honesty rules (ENGINEERING.md "no fake implementations"):
 * - Nothing is invented. A value the core never sent stays `null` and the UI shows "unknown".
 * - On disconnect, live-only facts (capture flags) are cleared instead of being kept as if current.
 */

import type { ChatSendResult, ChatStreamFrame } from '../../shared/generated/ipc';

export type HealthStatus = 'available' | 'limited' | 'unavailable';

export interface HealthComponent {
  component: string;
  status: string;
  reason: string;
}

export interface HealthState {
  components: HealthComponent[];
  generatedAt: string | null;
}

export interface Provider {
  id: string;
  displayName: string;
  local: boolean;
  roles: string[];
  status: string;
  reason: string;
}

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

// ---- Stream Bot (Spec v0.2, EPIC-11) --------------------------------------------------------

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

export interface StreamState {
  session: StreamSessionStatusState;
  chat: StreamChatEntry[];
  funkenTop: FunkenTopEntry[];
}

export const INITIAL_STREAM_STATE: StreamState = {
  session: INITIAL_SESSION_STATUS,
  chat: [],
  funkenTop: [],
};

/** Keep the live chat feed bounded; the core owns the durable `chat_events` table. */
export const STREAM_CHAT_LIMIT = 200;

// ---- Clip Pipeline (Spec v0.6, EPIC-15) ---------------------------------------------------------

/** One `clips` row (`nox.ipc.protocol.ClipRecord`). */
export interface ClipRecord {
  id: string;
  source: string; // event | manual | marker_promoted
  triggerKind: string;
  originEventId: string;
  sessionId: string;
  filePath: string;
  durationS: number;
  createdAt: string;
  thumbnailPath: string | null;
  tags: string[];
  status: string; // new | reviewed | exported | discarded
  parentClipId: string | null;
  checksum: string;
  notes: string;
}

export interface ClipsState {
  items: ClipRecord[];
}

export const INITIAL_CLIPS_STATE: ClipsState = { items: [] };

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
};

export const PRIVACY_MODES = ['full', 'balanced', 'private', 'offline'] as const;
export type PrivacyMode = (typeof PRIVACY_MODES)[number];

export const MODES = [
  'companion', 'coding', 'project', 'stream', 'rocket_league', 'creative', 'research', 'focus', 'idle',
] as const;

/** Keep the live audit list bounded; the core owns the complete append-only log. */
export const AUDIT_LIMIT = 500;

type Rec = Record<string, unknown>;

const isRec = (x: unknown): x is Rec => typeof x === 'object' && x !== null && !Array.isArray(x);
const str = (x: unknown, fallback = ''): string => (typeof x === 'string' ? x : fallback);
const bool = (x: unknown): boolean => x === true;
const numOrNull = (x: unknown): number | null =>
  typeof x === 'number' && Number.isFinite(x) ? x : null;

/** `health.get` response / `health.report` event → HealthReport (components: dict[str, HealthChanged]). */
export function parseHealthReport(payload: unknown): HealthState | null {
  if (!isRec(payload)) return null;
  const raw = payload.components;
  if (!isRec(raw)) return null;
  const components: HealthComponent[] = Object.entries(raw).map(([name, value]) => ({
    component: isRec(value) ? str(value.component, name) : name,
    status: isRec(value) ? str(value.status, 'unavailable') : 'unavailable',
    reason: isRec(value) ? str(value.reason) : '',
  }));
  components.sort((a, b) => a.component.localeCompare(b.component));
  return { components, generatedAt: typeof payload.generated_at === 'string' ? payload.generated_at : null };
}

/** `ai.providers` response: a list of ProviderInfo, carried as `{providers: [...]}`. */
export function parseProviders(payload: unknown): Provider[] | null {
  if (!isRec(payload)) return null;
  const list = Array.isArray(payload.providers)
    ? payload.providers
    : Array.isArray(payload.items)
      ? payload.items
      : null;
  if (!list) return null;
  const out: Provider[] = [];
  for (const entry of list) {
    if (!isRec(entry)) continue;
    const id = str(entry.id);
    if (!id) continue;
    out.push({
      id,
      displayName: str(entry.display_name, id),
      local: bool(entry.local),
      roles: Array.isArray(entry.roles) ? entry.roles.filter((r): r is string => typeof r === 'string') : [],
      status: str(entry.status, 'unavailable'),
      reason: str(entry.reason),
    });
  }
  return out;
}

/** `state.get` response: a NoxState snapshot (or subtree). Missing branches leave values untouched. */
export function applyStateSnapshot(state: DashboardState, payload: unknown): DashboardState {
  if (!isRec(payload)) return state;
  const snap = isRec(payload.state) ? payload.state : payload;
  let next = state;
  const assistant = snap.assistant;
  if (isRec(assistant)) {
    next = {
      ...next,
      mode: str(assistant.mode, next.mode ?? ''),
      muted: typeof assistant.muted === 'boolean' ? assistant.muted : next.muted,
    };
  }
  const privacy = snap.privacy;
  if (isRec(privacy)) {
    next = {
      ...next,
      privacyMode: str(privacy.mode, next.privacyMode ?? ''),
      capture: {
        microphone: bool(privacy.microphone),
        camera: bool(privacy.camera),
        screen: bool(privacy.screen),
        cloud: bool(privacy.cloud_request_active),
      },
    };
  }
  const voice = snap.voice;
  if (isRec(voice)) {
    next = {
      ...next,
      voice: {
        sttEngine: str(voice.stt_engine),
        ttsEngine: str(voice.tts_engine),
        listening: bool(voice.listening),
        speaking: bool(voice.speaking),
        routing: str(voice.routing),
        lastLatencyMs: numOrNull(voice.last_transcript_latency_ms),
      },
    };
  }
  const system = snap.system;
  if (isRec(system)) next = { ...next, systemLevel: str(system.level, next.systemLevel ?? '') };
  return next;
}

/** One entry of a `clip.list {}` response (`nox.ipc.protocol.ClipRecord`). `null` when the entry
 * is missing its required fields, rather than inventing placeholders. */
function parseClipRecord(entry: unknown): ClipRecord | null {
  if (!isRec(entry)) return null;
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
    thumbnailPath: typeof entry.thumbnail_path === 'string' ? entry.thumbnail_path : null,
    tags: Array.isArray(entry.tags) ? entry.tags.filter((x): x is string => typeof x === 'string') : [],
    status: str(entry.status, 'new'),
    parentClipId: typeof entry.parent_clip_id === 'string' ? entry.parent_clip_id : null,
    checksum: str(entry.checksum),
    notes: str(entry.notes),
  };
}

/** `clip.list {status?, limit?}` response: `nox.ipc.protocol.ClipListResult`. */
export function parseClipList(payload: unknown): ClipRecord[] | null {
  if (!isRec(payload) || !Array.isArray(payload.clips)) return null;
  return payload.clips.map(parseClipRecord).filter((c): c is ClipRecord => c !== null);
}

export function applyClipList(state: DashboardState, payload: unknown): DashboardState {
  const items = parseClipList(payload);
  return items ? { ...state, clips: { items } } : state;
}

/** Upsert one clip (by id) into the live list - used both for `clip.tag`/`clip.export` responses
 * and for the `clip.saved`/`clip.exported` events below. */
function upsertClip(state: DashboardState, clip: ClipRecord): DashboardState {
  const items = state.clips.items.some((c) => c.id === clip.id)
    ? state.clips.items.map((c) => (c.id === clip.id ? clip : c))
    : [clip, ...state.clips.items];
  return { ...state, clips: { items } };
}

export function applyClipRecord(state: DashboardState, payload: unknown): DashboardState {
  if (!isRec(payload)) return state;
  const clip = parseClipRecord(payload.clip);
  return clip ? upsertClip(state, clip) : state;
}

// ---- Proactive notifications (Spec v0.5 EPIC-19/ST-08-01): toast panel -----------------------

export interface ProactiveNotification {
  id: string;
  kind: string; // "urgent" | "proactive"
  priority: string;
  text: string;
  channel: string; // "speech" | "toast" | "speech+toast"
  spoken: boolean;
  announced: boolean;
  ts: string;
}

/** Keep the live toast list bounded; nothing here is a durable log (see `nox.proactive.store`). */
export const NOTIFICATION_LIMIT = 20;

let notificationSeq = 0;

function notificationRow(payload: Rec, ts: string): ProactiveNotification | null {
  const text = str(payload.text);
  if (!text) return null; // suppressed/empty notifications never reach the dashboard as toasts
  notificationSeq += 1;
  return {
    id: `${ts || 'n'}-${notificationSeq}`,
    kind: str(payload.kind),
    priority: str(payload.priority),
    text,
    channel: str(payload.channel),
    spoken: bool(payload.spoken),
    announced: bool(payload.announced),
    ts,
  };
}

/** Local-only dismissal (no server round trip - the notification store is the core's, this is
 * just "stop showing this toast"). */
export function dismissNotification(state: DashboardState, id: string): DashboardState {
  const notifications = state.notifications.filter((n) => n.id !== id);
  return notifications.length === state.notifications.length ? state : { ...state, notifications };
}

// ---- Dashboard Settings view (ST-08): health history + effective config -----------------------

export interface HealthHistoryEntry {
  id: number;
  ts: string;
  component: string;
  status: string;
  reason: string;
}

/** `health.history {}` response (`nox.ipc.protocol.HealthHistoryResult`). */
export function parseHealthHistory(payload: unknown): HealthHistoryEntry[] {
  if (!isRec(payload) || !Array.isArray(payload.entries)) return [];
  const out: HealthHistoryEntry[] = [];
  for (const entry of payload.entries) {
    if (!isRec(entry)) continue;
    const id = numOrNull(entry.id);
    if (id === null) continue;
    out.push({
      id,
      ts: str(entry.ts),
      component: str(entry.component),
      status: str(entry.status, 'unavailable'),
      reason: str(entry.reason),
    });
  }
  return out;
}

/** `config.effective {}` response (`nox.ipc.protocol.ConfigEffective`): a redacted JSON tree. */
export function parseConfigEffective(payload: unknown): Rec | null {
  if (!isRec(payload) || !isRec(payload.config)) return null;
  return payload.config;
}

// ---- Editable settings (`config.*`, `secrets.*`, `twitch.auth.*`, `personality.*`) -------------

export type SettingType = 'string' | 'int' | 'float' | 'bool' | 'enum' | 'list[str]';

const SETTING_TYPES: readonly string[] = ['string', 'int', 'float', 'bool', 'enum', 'list[str]'];

export interface SettingSpec {
  path: string;
  type: SettingType;
  options: string[];
  min: number | null;
  max: number | null;
  restartRequired: boolean;
  group: string;
}

export interface EditableConfig {
  values: Record<string, unknown>;
  schema: SettingSpec[];
  userConfigPath: string;
}

/**
 * `config.get {}` response: `{values, schema, user_config_path}`. An entry without a usable `path`
 * or with a type this UI cannot render is dropped rather than guessed into a text field.
 */
export function parseEditableConfig(payload: unknown): EditableConfig | null {
  if (!isRec(payload)) return null;
  const rawSchema = payload.schema;
  if (!Array.isArray(rawSchema)) return null;
  const schema: SettingSpec[] = [];
  for (const entry of rawSchema) {
    if (!isRec(entry)) continue;
    const path = str(entry.path);
    const type = str(entry.type);
    if (!path || !SETTING_TYPES.includes(type)) continue;
    schema.push({
      path,
      type: type as SettingType,
      options: Array.isArray(entry.options)
        ? entry.options.map((o) => String(o)).filter((o) => o.length > 0)
        : [],
      min: numOrNull(entry.min),
      max: numOrNull(entry.max),
      restartRequired: bool(entry.restart_required),
      group: str(entry.group, 'identity'),
    });
  }
  return {
    values: isRec(payload.values) ? payload.values : {},
    schema,
    userConfigPath: str(payload.user_config_path),
  };
}

export interface ConfigSetResult {
  ok: boolean;
  applied: string[];
  restartRequired: string[];
  errors: Record<string, string>;
}

/** `config.set {values}` response: `{ok, applied, restart_required, errors}`. */
export function parseConfigSetResult(payload: unknown): ConfigSetResult {
  const rec = isRec(payload) ? payload : {};
  const list = (x: unknown): string[] =>
    Array.isArray(x) ? x.filter((v): v is string => typeof v === 'string') : [];
  const errors: Record<string, string> = {};
  if (isRec(rec.errors)) {
    for (const [path, message] of Object.entries(rec.errors)) errors[path] = String(message);
  }
  return {
    ok: rec.ok !== false && Object.keys(errors).length === 0,
    applied: list(rec.applied),
    restartRequired: list(rec.restart_required),
    errors,
  };
}

export interface SecretStatus {
  name: string;
  present: boolean;
  group: string;
}

/** `secrets.status {}` response: `{secrets: [{name, present, group}]}`. Values are never sent. */
export function parseSecretStatus(payload: unknown): SecretStatus[] | null {
  if (!isRec(payload) || !Array.isArray(payload.secrets)) return null;
  const out: SecretStatus[] = [];
  for (const entry of payload.secrets) {
    if (!isRec(entry)) continue;
    const name = str(entry.name);
    if (!name) continue;
    out.push({ name, present: bool(entry.present), group: str(entry.group) });
  }
  return out;
}

export interface TwitchDeviceCode {
  userCode: string;
  verificationUri: string;
  expiresIn: number;
  /** Seconds between two `twitch.auth.status` polls, as dictated by the core. */
  interval: number;
}

/** `twitch.auth.start {}` response. Returns null when no usable code came back. */
export function parseTwitchDeviceCode(payload: unknown): TwitchDeviceCode | null {
  if (!isRec(payload)) return null;
  const userCode = str(payload.user_code);
  const verificationUri = str(payload.verification_uri);
  if (!userCode || !verificationUri) return null;
  return {
    userCode,
    verificationUri,
    expiresIn: numOrNull(payload.expires_in) ?? 0,
    interval: Math.max(1, numOrNull(payload.interval) ?? 5),
  };
}

export interface TwitchAuthStatus {
  state: string; // idle | pending | authorized | expired | error
  login: string;
  expiresAt: string;
  scopes: string[];
  error: string;
}

/** `twitch.auth.status {}` response. An unknown `state` is passed through verbatim. */
export function parseTwitchAuthStatus(payload: unknown): TwitchAuthStatus | null {
  if (!isRec(payload)) return null;
  const state = str(payload.state);
  if (!state) return null;
  return {
    state,
    login: str(payload.login),
    expiresAt: str(payload.expires_at),
    scopes: Array.isArray(payload.scopes)
      ? payload.scopes.filter((s): s is string => typeof s === 'string')
      : [],
    error: str(payload.error),
  };
}

/** `personality.get {}` response: `{text, path}`. */
export function parsePersonality(payload: unknown): { text: string; path: string } | null {
  if (!isRec(payload) || typeof payload.text !== 'string') return null;
  return { text: payload.text, path: str(payload.path) };
}

function auditRow(payload: Rec, ts: string): AuditRow | null {
  const seq = numOrNull(payload.seq);
  if (seq === null) return null;
  return {
    seq,
    actor: str(payload.actor),
    tool: str(payload.tool),
    action: str(payload.action),
    target: str(payload.target),
    decision: str(payload.decision),
    result: str(payload.result),
    taskId: typeof payload.task_id === 'string' ? payload.task_id : null,
    ts,
  };
}

/** `stream.session.status {}` response: `nox.ipc.protocol.StreamSessionStatus`. */
export function parseStreamSessionStatus(payload: unknown): StreamSessionStatusState | null {
  if (!isRec(payload)) return null;
  const plugins = isRec(payload.plugins) ? payload.plugins : {};
  return {
    active: bool(payload.active),
    sessionId: typeof payload.session_id === 'string' ? payload.session_id : null,
    startedAt: typeof payload.started_at === 'string' ? payload.started_at : null,
    scene: typeof payload.scene === 'string' ? payload.scene : null,
    plugins: {
      obs: str(plugins.obs, 'unknown'),
      twitch: str(plugins.twitch, 'unknown'),
    },
  };
}

/** `stream.funken.top {limit}` response: `nox.ipc.protocol.FunkenTop`. */
export function parseFunkenTop(payload: unknown): FunkenTopEntry[] | null {
  if (!isRec(payload) || !Array.isArray(payload.viewers)) return null;
  const out: FunkenTopEntry[] = [];
  for (const entry of payload.viewers) {
    if (!isRec(entry)) continue;
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

export function applyStreamSessionStatus(state: DashboardState, payload: unknown): DashboardState {
  const parsed = parseStreamSessionStatus(payload);
  return parsed ? { ...state, stream: { ...state.stream, session: parsed } } : state;
}

export function applyFunkenTop(state: DashboardState, payload: unknown): DashboardState {
  const parsed = parseFunkenTop(payload);
  return parsed ? { ...state, stream: { ...state.stream, funkenTop: parsed } } : state;
}

/** Apply one received event. Unknown names leave the state untouched (identity, so React can skip). */
export function reduceEvent(
  state: DashboardState,
  name: string,
  payload: Record<string, unknown>,
  ts = '',
): DashboardState {
  switch (name) {
    case 'health.report': {
      const health = parseHealthReport(payload);
      return health ? { ...state, health } : state;
    }
    case 'system.health_changed': {
      const component = str(payload.component);
      if (!component) return state;
      const entry: HealthComponent = {
        component,
        status: str(payload.status, 'unavailable'),
        reason: str(payload.reason),
      };
      const components = (state.health?.components ?? []).filter((c) => c.component !== component);
      components.push(entry);
      components.sort((a, b) => a.component.localeCompare(b.component));
      return { ...state, health: { components, generatedAt: state.health?.generatedAt ?? null } };
    }
    case 'system.mode_changed':
      return { ...state, mode: str(payload.current, state.mode ?? '') };
    case 'privacy.mode_changed':
      return { ...state, privacyMode: str(payload.current, state.privacyMode ?? '') };
    case 'privacy.capture_changed':
      return {
        ...state,
        capture: {
          microphone: bool(payload.microphone),
          camera: bool(payload.camera),
          screen: bool(payload.screen),
          cloud: bool(payload.cloud),
        },
      };
    case 'voice.muted':
      return { ...state, muted: payload.muted !== false };
    case 'voice.input_started':
      return state.voice ? { ...state, voice: { ...state.voice, listening: true } } : state;
    case 'voice.input_stopped':
      return state.voice ? { ...state, voice: { ...state.voice, listening: false } } : state;
    case 'tts.started':
      return state.voice ? { ...state, voice: { ...state.voice, speaking: true } } : state;
    case 'tts.finished':
    case 'tts.interrupted':
      return state.voice ? { ...state, voice: { ...state.voice, speaking: false } } : state;
    case 'ai.provider_changed':
      return state; // the provider list is refreshed via ai.providers, never guessed from an event
    case 'settings.changed':
      // Which paths changed is the core's business; the Settings page reloads the whole snapshot
      // rather than patching a value it did not read back.
      return { ...state, settingsRevision: state.settingsRevision + 1 };
    case 'twitch.auth.changed': {
      const twitchAuth = str(payload.state);
      return twitchAuth ? { ...state, twitchAuth } : state;
    }
    case 'security.kill_switch':
      return { ...state, systemLevel: 'safe_mode' };
    case 'system.started':
      return { ...state, systemLevel: 'running' };
    case 'system.stopping':
      return { ...state, systemLevel: 'stopping' };
    case 'proactive.notification': {
      const row = notificationRow(payload, ts);
      if (!row) return state;
      const notifications = [row, ...state.notifications].slice(0, NOTIFICATION_LIMIT);
      return { ...state, notifications };
    }
    case 'security.audit': {
      const row = auditRow(payload, ts);
      if (!row) return state;
      if (state.audit.some((r) => r.seq === row.seq)) return state;
      const audit = [row, ...state.audit].slice(0, AUDIT_LIMIT);
      return { ...state, audit };
    }
    case 'stream.started':
      return {
        ...state,
        stream: {
          ...state.stream,
          session: {
            ...state.stream.session,
            active: true,
            sessionId: str(payload.session_id, state.stream.session.sessionId ?? ''),
          },
        },
      };
    case 'stream.ended':
      return {
        ...state,
        stream: { ...state.stream, session: { ...INITIAL_SESSION_STATUS } },
      };
    case 'obs.scene_changed':
      return {
        ...state,
        stream: {
          ...state.stream,
          session: { ...state.stream.session, scene: str(payload.current_scene) || null },
        },
      };
    case 'obs.connected':
    case 'obs.disconnected':
      return {
        ...state,
        stream: {
          ...state.stream,
          session: {
            ...state.stream.session,
            plugins: {
              ...state.stream.session.plugins,
              obs: name === 'obs.connected' ? 'connected' : 'disconnected',
            },
          },
        },
      };
    case 'twitch.connected':
    case 'twitch.disconnected':
      return {
        ...state,
        stream: {
          ...state.stream,
          session: {
            ...state.stream.session,
            plugins: {
              ...state.stream.session.plugins,
              twitch: name === 'twitch.connected' ? 'connected' : 'disconnected',
            },
          },
        },
      };
    case 'twitch.chat_message': {
      const text = str(payload.text);
      if (!text) return state;
      const entry: StreamChatEntry = {
        id: `${ts || Date.now()}-${state.stream.chat.length}`,
        viewerId: str(payload.viewer_id),
        text,
        addressed: bool(payload.addressed_to_nox),
        relevance: numOrNull(payload.relevance) ?? 0,
        ts,
      };
      const chat = [entry, ...state.stream.chat].slice(0, STREAM_CHAT_LIMIT);
      return { ...state, stream: { ...state.stream, chat } };
    }
    case 'state.changed': {
      const path = str(payload.path);
      const value = payload.new;
      if (path === 'assistant.muted') return { ...state, muted: value === true };
      if (path === 'assistant.mode' && typeof value === 'string') return { ...state, mode: value };
      if (path === 'privacy.mode' && typeof value === 'string') return { ...state, privacyMode: value };
      if (path === 'system.level' && typeof value === 'string') return { ...state, systemLevel: value };
      if (path.startsWith('privacy.') && state.capture) {
        const field = path.slice('privacy.'.length);
        const key =
          field === 'cloud_request_active' ? 'cloud' : (field as keyof CaptureFlags);
        if (key in state.capture) return { ...state, capture: { ...state.capture, [key]: value === true } };
      }
      return state;
    }
    case 'clip.saved': {
      const clipId = str(payload.clip_id);
      if (!clipId) return state;
      const clip: ClipRecord = {
        id: clipId,
        source: str(payload.source),
        triggerKind: str(payload.trigger_kind),
        originEventId: '',
        sessionId: '',
        filePath: str(payload.file_path),
        durationS: numOrNull(payload.duration_s) ?? 0,
        createdAt: ts,
        thumbnailPath: null,
        tags: Array.isArray(payload.tags) ? payload.tags.filter((x): x is string => typeof x === 'string') : [],
        status: 'new',
        parentClipId: null,
        checksum: '',
        notes: '',
      };
      return upsertClip(state, clip);
    }
    case 'clip.exported': {
      const clipId = str(payload.clip_id);
      const existing = state.clips.items.find((c) => c.id === clipId);
      if (!existing) return state;
      return upsertClip(state, { ...existing, status: 'exported' });
    }
    default:
      return state;
  }
}

/** Connection changes. Offline clears facts that are only true "right now". */
export function setConnected(state: DashboardState, connected: boolean): DashboardState {
  if (connected === state.connected) return state;
  if (connected) return { ...state, connected };
  // Plugin connection state is only true "right now" - the chat feed and Funken leaderboard stay
  // as the last known snapshot, same treatment as `capture`.
  return {
    ...state,
    connected,
    capture: null,
    stream: { ...state.stream, session: { ...state.stream.session, plugins: INITIAL_PLUGIN_STATUS } },
  };
}

// ---- chat ------------------------------------------------------------------------------------

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
 * (src/nox/ipc/protocol.py, generated at ui/shared/generated/ipc.ts). OP-9 fixed this shape; the
 * hub validates every stream frame against it, so there is no `chunk`/`text` fallback to read here.
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

// ---- kill switch confirm -----------------------------------------------------------------------

export type KillPhase = 'idle' | 'confirm' | 'sent';

/**
 * Two-step kill switch (the button can never fire on a single stray click, and the confirm step
 * expires so a forgotten armed button does not stay dangerous).
 */
export function killNext(phase: KillPhase, action: 'press' | 'cancel' | 'timeout'): KillPhase {
  if (action === 'cancel' || action === 'timeout') return 'idle';
  if (phase === 'idle') return 'confirm';
  if (phase === 'confirm') return 'sent';
  return phase;
}

export const KILL_CONFIRM_MS = 6000;
