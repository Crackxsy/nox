/**
 * The one writer of `DashboardState`: `reduceEvent` maps a received IPC event onto a new state and
 * delegates the work to the per-domain modules next to this file. An unknown event name returns the
 * state unchanged (identity, so React can skip the render).
 */

import { bool, isRecord, numOrNull, str, strOrNull } from '../../../shared/guards';
import {
  applyClipExported,
  applyClipList,
  applyClipRecord,
  applyClipSaved,
  parseClipRecord,
} from './clips';
import { applyHealthChanged, parseHealthReport } from './health';
import { applyHomeConnected, applyHomeDisconnected, applyHomeStateChanged } from './home';
import { NOTIFICATION_LIMIT, dismiss, notificationRow } from './notifications';
import {
  AUDIT_LIMIT,
  type AuditRow,
  type CaptureFlags,
  type DashboardState,
} from './state';
import {
  INITIAL_SESSION_STATUS,
  STREAM_CHAT_LIMIT,
  chatEntry,
  parseFunkenTop,
  parseStreamSessionStatus,
} from './stream';

/** `state.get` response: a NoxState snapshot (or subtree). Missing branches leave values untouched. */
export function applyStateSnapshot(state: DashboardState, payload: unknown): DashboardState {
  if (!isRecord(payload)) return state;
  const snap = isRecord(payload.state) ? payload.state : payload;
  let next = state;
  const assistant = snap.assistant;
  if (isRecord(assistant)) {
    next = {
      ...next,
      mode: strOrNull(assistant.mode) ?? next.mode,
      muted: typeof assistant.muted === 'boolean' ? assistant.muted : next.muted,
    };
  }
  const privacy = snap.privacy;
  if (isRecord(privacy)) {
    next = {
      ...next,
      privacyMode: strOrNull(privacy.mode) ?? next.privacyMode,
      capture: {
        microphone: bool(privacy.microphone),
        camera: bool(privacy.camera),
        screen: bool(privacy.screen),
        cloud: bool(privacy.cloud_request_active),
      },
    };
  }
  const voice = snap.voice;
  if (isRecord(voice)) {
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
  if (isRecord(system)) next = { ...next, systemLevel: strOrNull(system.level) ?? next.systemLevel };
  return next;
}

export function applyStreamSessionStatus(state: DashboardState, payload: unknown): DashboardState {
  const parsed = parseStreamSessionStatus(payload);
  return parsed ? { ...state, stream: { ...state.stream, session: parsed } } : state;
}

export function applyFunkenTop(state: DashboardState, payload: unknown): DashboardState {
  const parsed = parseFunkenTop(payload);
  return parsed ? { ...state, stream: { ...state.stream, funkenTop: parsed } } : state;
}

export function applyClipListResult(state: DashboardState, payload: unknown): DashboardState {
  const clips = applyClipList(state.clips, payload);
  return clips === state.clips ? state : { ...state, clips };
}

export function applyClipTagResult(state: DashboardState, clip: unknown): DashboardState {
  const parsed = parseClipRecord(clip);
  return parsed ? { ...state, clips: applyClipRecord(state.clips, parsed) } : state;
}

/** Local-only toast dismissal (the notification store is the core's; this just stops showing it). */
export function dismissNotification(state: DashboardState, id: string): DashboardState {
  const notifications = dismiss(state.notifications, id);
  return notifications ? { ...state, notifications } : state;
}

function auditRow(payload: Record<string, unknown>, ts: string): AuditRow | null {
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
    taskId: strOrNull(payload.task_id),
    ts,
  };
}

function withPlugin(
  state: DashboardState,
  plugin: 'obs' | 'twitch',
  status: string,
): DashboardState {
  return {
    ...state,
    stream: {
      ...state.stream,
      session: {
        ...state.stream.session,
        plugins: { ...state.stream.session.plugins, [plugin]: status },
      },
    },
  };
}

/** Apply one received event. Unknown names leave the state untouched. */
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
      const health = applyHealthChanged(state.health, payload);
      // Identity when nothing changed (an event without a component name), so React can skip.
      return health === null || health === state.health ? state : { ...state, health };
    }
    case 'system.mode_changed':
      return { ...state, mode: strOrNull(payload.current) ?? state.mode };
    case 'privacy.mode_changed':
      return { ...state, privacyMode: strOrNull(payload.current) ?? state.privacyMode };
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
      const seq = state.seq + 1;
      const row = notificationRow(payload, ts, seq);
      if (!row) return state;
      return {
        ...state,
        seq,
        notifications: [row, ...state.notifications].slice(0, NOTIFICATION_LIMIT),
      };
    }
    case 'security.audit': {
      const row = auditRow(payload, ts);
      if (!row) return state;
      if (state.audit.some((r) => r.seq === row.seq)) return state;
      return { ...state, audit: [row, ...state.audit].slice(0, AUDIT_LIMIT) };
    }
    case 'plugin.failed': {
      // The Stream page needs a reason it can show; the core words it, this only carries it.
      const reason = str(payload.reason);
      return reason ? { ...state, stream: { ...state.stream, pluginReason: reason } } : state;
    }
    case 'stream.started':
      return {
        ...state,
        stream: {
          ...state.stream,
          session: {
            ...state.stream.session,
            active: true,
            sessionId: strOrNull(payload.session_id) ?? state.stream.session.sessionId,
          },
        },
      };
    case 'stream.ended':
      return { ...state, stream: { ...state.stream, session: { ...INITIAL_SESSION_STATUS } } };
    case 'obs.scene_changed':
      return {
        ...state,
        stream: {
          ...state.stream,
          session: { ...state.stream.session, scene: strOrNull(payload.current_scene) },
        },
      };
    case 'obs.connected':
    case 'obs.disconnected':
      return withPlugin(state, 'obs', name === 'obs.connected' ? 'connected' : 'disconnected');
    case 'twitch.connected':
    case 'twitch.disconnected':
      return withPlugin(state, 'twitch', name === 'twitch.connected' ? 'connected' : 'disconnected');
    case 'twitch.chat_message': {
      const seq = state.seq + 1;
      const entry = chatEntry(payload, ts, seq);
      if (!entry) return state;
      return {
        ...state,
        seq,
        stream: {
          ...state.stream,
          chat: [entry, ...state.stream.chat].slice(0, STREAM_CHAT_LIMIT),
        },
      };
    }
    case 'state.changed': {
      const path = str(payload.path);
      const value = payload.new;
      if (path === 'assistant.muted') return { ...state, muted: value === true };
      if (path === 'assistant.mode' && typeof value === 'string') return { ...state, mode: value };
      if (path === 'privacy.mode' && typeof value === 'string')
        return { ...state, privacyMode: value };
      if (path === 'system.level' && typeof value === 'string')
        return { ...state, systemLevel: value };
      if (path.startsWith('privacy.') && state.capture) {
        const field = path.slice('privacy.'.length);
        const key = field === 'cloud_request_active' ? 'cloud' : (field as keyof CaptureFlags);
        if (key in state.capture) {
          return { ...state, capture: { ...state.capture, [key]: value === true } };
        }
      }
      return state;
    }
    case 'home.connected': {
      const home = applyHomeConnected(state.home, payload);
      return home === state.home ? state : { ...state, home };
    }
    case 'home.disconnected': {
      const home = applyHomeDisconnected(state.home, payload);
      return home === state.home ? state : { ...state, home };
    }
    case 'home.state_changed': {
      const home = applyHomeStateChanged(state.home, payload);
      return home === state.home ? state : { ...state, home };
    }
    case 'clip.saved': {
      const clips = applyClipSaved(state.clips, payload, ts);
      return clips === state.clips ? state : { ...state, clips };
    }
    case 'clip.exported': {
      const clips = applyClipExported(state.clips, str(payload.clip_id));
      return clips === state.clips ? state : { ...state, clips };
    }
    default:
      return state;
  }
}
