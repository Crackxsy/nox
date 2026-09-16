import { describe, expect, it } from 'vitest';

import {
  AUDIT_LIMIT,
  type DashboardState,
  INITIAL_STATE,
  STREAM_CHAT_LIMIT,
  applyClipList,
  applyFunkenTop,
  applyStateSnapshot,
  applyStreamSessionStatus,
  finalAnswer,
  killNext,
  parseClipList,
  parseFunkenTop,
  parseHealthReport,
  parseProviders,
  parseStreamSessionStatus,
  reduceEvent,
  setConnected,
  streamDelta,
} from '../model';

describe('parseHealthReport', () => {
  it('maps the HealthReport payload to a sorted component list', () => {
    const health = parseHealthReport({
      components: {
        voice: { component: 'voice', status: 'limited', reason: 'no mic' },
        ai: { component: 'ai', status: 'available', reason: '' },
      },
      generated_at: '2026-09-11T10:00:00Z',
    });
    expect(health?.components.map((c) => c.component)).toEqual(['ai', 'voice']);
    expect(health?.components[1]).toEqual({ component: 'voice', status: 'limited', reason: 'no mic' });
    expect(health?.generatedAt).toBe('2026-09-11T10:00:00Z');
  });
  it('rejects junk instead of inventing a report', () => {
    expect(parseHealthReport(null)).toBeNull();
    expect(parseHealthReport({ components: [] })).toBeNull();
    expect(parseHealthReport({})).toBeNull();
    const partial = parseHealthReport({ components: { db: 'broken' } });
    expect(partial?.components).toEqual([{ component: 'db', status: 'unavailable', reason: '' }]);
    expect(partial?.generatedAt).toBeNull();
  });
});

describe('parseProviders', () => {
  it('reads ProviderInfo entries and skips unusable ones', () => {
    const list = parseProviders({
      providers: [
        { id: 'claude_code', display_name: 'Claude Code', local: false, roles: ['chat', 'code'], status: 'available' },
        { id: 'ollama', local: true, roles: ['classify', 7], status: 'limited', reason: 'cold' },
        { display_name: 'no id' },
        'nope',
      ],
    });
    expect(list).toEqual([
      { id: 'claude_code', displayName: 'Claude Code', local: false, roles: ['chat', 'code'], status: 'available', reason: '' },
      { id: 'ollama', displayName: 'ollama', local: true, roles: ['classify'], status: 'limited', reason: 'cold' },
    ]);
  });
  it('returns null when there is no list at all', () => {
    expect(parseProviders({})).toBeNull();
    expect(parseProviders(undefined)).toBeNull();
  });
});

describe('applyStateSnapshot', () => {
  it('reads mode, privacy, capture, voice and system level from a NoxState snapshot', () => {
    const s = applyStateSnapshot(INITIAL_STATE, {
      assistant: { mode: 'coding', muted: true },
      privacy: { mode: 'private', microphone: true, camera: false, screen: false, cloud_request_active: true },
      voice: { stt_engine: 'whisper', tts_engine: 'piper', listening: false, speaking: true, routing: 'private', last_transcript_latency_ms: 420 },
      system: { level: 'running' },
    });
    expect(s.mode).toBe('coding');
    expect(s.muted).toBe(true);
    expect(s.privacyMode).toBe('private');
    expect(s.capture).toEqual({ microphone: true, camera: false, screen: false, cloud: true });
    expect(s.voice).toEqual({
      sttEngine: 'whisper', ttsEngine: 'piper', listening: false, speaking: true,
      routing: 'private', lastLatencyMs: 420,
    });
    expect(s.systemLevel).toBe('running');
  });
  it('accepts a wrapped {state: ...} payload and leaves missing branches alone', () => {
    const s = applyStateSnapshot(INITIAL_STATE, { state: { assistant: { mode: 'stream' } } });
    expect(s.mode).toBe('stream');
    expect(s.privacyMode).toBeNull();
    expect(s.voice).toBeNull();
    expect(applyStateSnapshot(INITIAL_STATE, 'nope')).toBe(INITIAL_STATE);
  });
});

describe('reduceEvent', () => {
  it('updates privacy, capture, mode, mute and system level', () => {
    let s = reduceEvent(INITIAL_STATE, 'privacy.mode_changed', { previous: 'balanced', current: 'offline', by: 'user' });
    expect(s.privacyMode).toBe('offline');
    s = reduceEvent(s, 'privacy.capture_changed', { microphone: true, camera: false, screen: 1, cloud: false });
    expect(s.capture).toEqual({ microphone: true, camera: false, screen: false, cloud: false });
    s = reduceEvent(s, 'system.mode_changed', { previous: 'idle', current: 'coding' });
    expect(s.mode).toBe('coding');
    s = reduceEvent(s, 'voice.muted', { muted: true });
    expect(s.muted).toBe(true);
    s = reduceEvent(s, 'security.kill_switch', { by: 'ui', reason: 'test' });
    expect(s.systemLevel).toBe('safe_mode');
    expect(reduceEvent(s, 'nothing.known', {})).toBe(s);
  });

  it('merges system.health_changed into the component list', () => {
    let s = reduceEvent(INITIAL_STATE, 'health.report', {
      components: { ai: { component: 'ai', status: 'available', reason: '' } },
    });
    s = reduceEvent(s, 'system.health_changed', { component: 'voice', status: 'unavailable', reason: 'no device' });
    expect(s.health?.components.map((c) => `${c.component}:${c.status}`)).toEqual([
      'ai:available', 'voice:unavailable',
    ]);
    s = reduceEvent(s, 'system.health_changed', { component: 'voice', status: 'limited', reason: '' });
    expect(s.health?.components).toHaveLength(2);
    expect(s.health?.components[1].status).toBe('limited');
    expect(reduceEvent(s, 'system.health_changed', {})).toBe(s);
  });

  it('collects audit entries newest first, without duplicates and bounded', () => {
    let s = INITIAL_STATE;
    for (const seq of [1, 2, 3, 2]) {
      s = reduceEvent(s, 'security.audit', {
        seq, actor: 'agent', tool: 'fs', action: 'write', target: 'a.txt',
        decision: 'allow', result: 'ok', prev_hash: '', hash: '',
      }, '2026-09-11T10:00:00Z');
    }
    expect(s.audit.map((r) => r.seq)).toEqual([3, 2, 1]);
    expect(s.audit[0].ts).toBe('2026-09-11T10:00:00Z');
    expect(reduceEvent(s, 'security.audit', { actor: 'x' })).toBe(s);

    let big: DashboardState = INITIAL_STATE;
    for (let i = 0; i < AUDIT_LIMIT + 25; i++) {
      big = reduceEvent(big, 'security.audit', { seq: i, result: 'ok' });
    }
    expect(big.audit).toHaveLength(AUDIT_LIMIT);
    expect(big.audit[0].seq).toBe(AUDIT_LIMIT + 24);
  });

  it('applies state.changed paths including capture fields', () => {
    let s = applyStateSnapshot(INITIAL_STATE, { privacy: { mode: 'balanced' } });
    s = reduceEvent(s, 'state.changed', { path: 'privacy.microphone', old: false, new: true, version: 4 });
    expect(s.capture?.microphone).toBe(true);
    s = reduceEvent(s, 'state.changed', { path: 'privacy.cloud_request_active', old: false, new: true, version: 5 });
    expect(s.capture?.cloud).toBe(true);
    s = reduceEvent(s, 'state.changed', { path: 'assistant.mode', old: 'idle', new: 'focus', version: 6 });
    expect(s.mode).toBe('focus');
    s = reduceEvent(s, 'state.changed', { path: 'assistant.muted', old: false, new: true, version: 7 });
    expect(s.muted).toBe(true);
    expect(reduceEvent(s, 'state.changed', { path: 'user.activity', new: 'coding' })).toBe(s);
  });

  it('voice and tts events only touch a known voice state', () => {
    expect(reduceEvent(INITIAL_STATE, 'tts.started', {})).toBe(INITIAL_STATE);
    let s = applyStateSnapshot(INITIAL_STATE, { voice: { stt_engine: 'whisper' } });
    s = reduceEvent(s, 'tts.started', { text: 'hi', channel: 'private', engine: 'piper', utterance_id: 'u1' });
    expect(s.voice?.speaking).toBe(true);
    s = reduceEvent(s, 'tts.finished', {});
    expect(s.voice?.speaking).toBe(false);
    s = reduceEvent(s, 'voice.input_started', {});
    expect(s.voice?.listening).toBe(true);
  });
});

describe('setConnected', () => {
  it('clears capture when the connection drops (never claim stale capture state)', () => {
    let s = reduceEvent(INITIAL_STATE, 'privacy.capture_changed', { microphone: true });
    s = setConnected(s, true);
    expect(s.connected).toBe(true);
    expect(s.capture?.microphone).toBe(true);
    const off = setConnected(s, false);
    expect(off.connected).toBe(false);
    expect(off.capture).toBeNull();
    expect(setConnected(off, false)).toBe(off);
  });
});

describe('chat helpers', () => {
  it('reads the delta from a ChatStreamFrame and nothing else (OP-9: fixed shape)', () => {
    expect(streamDelta({ delta: 'a' })).toBe('a');
    expect(streamDelta({ delta: '' , done: true })).toBe('');
    expect(streamDelta({ done: true })).toBe('');
    expect(streamDelta({ chunk: 'b' })).toBe('');
    expect(streamDelta({ text: 'c' })).toBe('');
    expect(streamDelta({ delta: 42 })).toBe('');
  });
  it('reads the final ChatSendResult and reports a missing answer as missing', () => {
    expect(finalAnswer({ request_id: 'r1', text: 'hi', provider: 'ollama', degraded: true })).toEqual({
      text: 'hi', provider: 'ollama', degraded: true,
    });
    expect(finalAnswer({ done: true })).toEqual({
      text: null, provider: null, degraded: false,
    });
  });
});

describe('kill switch confirm', () => {
  it('needs two presses and can be cancelled or time out', () => {
    expect(killNext('idle', 'press')).toBe('confirm');
    expect(killNext('confirm', 'press')).toBe('sent');
    expect(killNext('confirm', 'cancel')).toBe('idle');
    expect(killNext('confirm', 'timeout')).toBe('idle');
    expect(killNext('sent', 'press')).toBe('sent');
  });
});

describe('parseStreamSessionStatus', () => {
  it('reads the StreamSessionStatus response', () => {
    const status = parseStreamSessionStatus({
      active: true,
      session_id: '7',
      started_at: '2026-09-14T18:00:00Z',
      scene: 'Just Chatting',
      plugins: { obs: 'connected', twitch: 'disconnected' },
    });
    expect(status).toEqual({
      active: true,
      sessionId: '7',
      startedAt: '2026-09-14T18:00:00Z',
      scene: 'Just Chatting',
      plugins: { obs: 'connected', twitch: 'disconnected' },
    });
  });
  it('defaults missing plugin statuses to unknown instead of inventing a value', () => {
    const status = parseStreamSessionStatus({ active: false });
    expect(status?.plugins).toEqual({ obs: 'unknown', twitch: 'unknown' });
    expect(status?.sessionId).toBeNull();
  });
  it('rejects junk', () => {
    expect(parseStreamSessionStatus(null)).toBeNull();
    expect(parseStreamSessionStatus('nope')).toBeNull();
  });
});

describe('parseFunkenTop', () => {
  it('reads FunkenTop viewers and skips unusable entries', () => {
    const top = parseFunkenTop({
      viewers: [
        { viewer_id: 'v1', display_name: 'Alice', balance: 120, tier: 'regular' },
        { display_name: 'no id' },
        'nope',
      ],
    });
    expect(top).toEqual([
      { viewerId: 'v1', displayName: 'Alice', balance: 120, tier: 'regular' },
    ]);
  });
  it('returns null when there is no viewers list at all', () => {
    expect(parseFunkenTop({})).toBeNull();
    expect(parseFunkenTop(undefined)).toBeNull();
  });
});

describe('stream reducers', () => {
  it('applyStreamSessionStatus / applyFunkenTop merge into state.stream', () => {
    let s = applyStreamSessionStatus(INITIAL_STATE, { active: true, session_id: '1' });
    expect(s.stream.session.active).toBe(true);
    s = applyFunkenTop(s, { viewers: [{ viewer_id: 'v1', balance: 5, tier: 'newcomer' }] });
    expect(s.stream.funkenTop).toEqual([
      { viewerId: 'v1', displayName: '', balance: 5, tier: 'newcomer' },
    ]);
  });

  it('stream.started/stream.ended flip session.active', () => {
    let s = reduceEvent(INITIAL_STATE, 'stream.started', { session_id: 'abc', mode: 'live' });
    expect(s.stream.session.active).toBe(true);
    expect(s.stream.session.sessionId).toBe('abc');
    s = reduceEvent(s, 'stream.ended', { session_id: 'abc' });
    expect(s.stream.session.active).toBe(false);
  });

  it('obs.connected/disconnected and twitch.connected/disconnected update plugin status', () => {
    let s = reduceEvent(INITIAL_STATE, 'obs.connected', {});
    expect(s.stream.session.plugins.obs).toBe('connected');
    s = reduceEvent(s, 'twitch.connected', {});
    expect(s.stream.session.plugins).toEqual({ obs: 'connected', twitch: 'connected' });
    s = reduceEvent(s, 'obs.disconnected', {});
    expect(s.stream.session.plugins.obs).toBe('disconnected');
  });

  it('obs.scene_changed updates the current scene', () => {
    const s = reduceEvent(INITIAL_STATE, 'obs.scene_changed', { current_scene: 'BRB' });
    expect(s.stream.session.scene).toBe('BRB');
  });

  it('twitch.chat_message prepends to the bounded live feed', () => {
    let s = INITIAL_STATE;
    s = reduceEvent(s, 'twitch.chat_message', {
      viewer_id: 'v1',
      text: 'hi nox',
      addressed_to_nox: true,
      relevance: 1,
    });
    expect(s.stream.chat).toHaveLength(1);
    expect(s.stream.chat[0]).toMatchObject({ viewerId: 'v1', text: 'hi nox', addressed: true });
    s = reduceEvent(s, 'twitch.chat_message', { viewer_id: 'v2', text: 'second', relevance: 0.2 });
    expect(s.stream.chat[0].text).toBe('second'); // newest first
    expect(s.stream.chat).toHaveLength(2);
    // empty text never produces an entry
    expect(reduceEvent(s, 'twitch.chat_message', { viewer_id: 'v3', text: '' }).stream.chat).toHaveLength(2);
  });

  it('bounds the live chat feed to STREAM_CHAT_LIMIT', () => {
    let s: DashboardState = INITIAL_STATE;
    for (let i = 0; i < STREAM_CHAT_LIMIT + 10; i++) {
      s = reduceEvent(s, 'twitch.chat_message', { viewer_id: 'v', text: `msg ${i}` });
    }
    expect(s.stream.chat).toHaveLength(STREAM_CHAT_LIMIT);
  });

  it('clears plugin status (but keeps the chat feed) when the connection drops', () => {
    let s = reduceEvent(INITIAL_STATE, 'obs.connected', {});
    s = reduceEvent(s, 'twitch.chat_message', { viewer_id: 'v1', text: 'hi' });
    s = setConnected(s, true);
    const off = setConnected(s, false);
    expect(off.stream.session.plugins).toEqual({ obs: 'unknown', twitch: 'unknown' });
    expect(off.stream.chat).toHaveLength(1);
  });
});

describe('clip.list / clip.saved / clip.exported (Spec v0.6 Clip Pipeline)', () => {
  it('parseClipList maps a clip.list response', () => {
    const clips = parseClipList({
      clips: [
        {
          id: 'c1',
          source: 'event',
          trigger_kind: 'rl.goal',
          origin_event_id: '',
          session_id: 's1',
          file_path: 'a.mp4',
          duration_s: 12.5,
          created_at: '2026-09-14T00:00:00Z',
          thumbnail_path: null,
          tags: ['goal'],
          status: 'new',
          parent_clip_id: null,
          checksum: 'x',
          notes: '',
        },
      ],
    });
    expect(clips).toEqual([
      {
        id: 'c1',
        source: 'event',
        triggerKind: 'rl.goal',
        originEventId: '',
        sessionId: 's1',
        filePath: 'a.mp4',
        durationS: 12.5,
        createdAt: '2026-09-14T00:00:00Z',
        thumbnailPath: null,
        tags: ['goal'],
        status: 'new',
        parentClipId: null,
        checksum: 'x',
        notes: '',
      },
    ]);
  });

  it('parseClipList returns null for a malformed payload', () => {
    expect(parseClipList({})).toBeNull();
    expect(parseClipList(undefined)).toBeNull();
  });

  it('applyClipList populates state.clips.items', () => {
    const s = applyClipList(INITIAL_STATE, {
      clips: [{ id: 'c1', source: 'event', trigger_kind: 'rl.goal', file_path: 'a.mp4' }],
    });
    expect(s.clips.items).toHaveLength(1);
    expect(s.clips.items[0].id).toBe('c1');
  });

  it('clip.saved prepends a new clip row', () => {
    const s = reduceEvent(INITIAL_STATE, 'clip.saved', {
      clip_id: 'c2',
      file_path: 'b.mp4',
      trigger_kind: 'chat_hype',
      source: 'event',
      duration_s: 5,
      tags: ['hype'],
    });
    expect(s.clips.items).toHaveLength(1);
    expect(s.clips.items[0]).toMatchObject({ id: 'c2', triggerKind: 'chat_hype', status: 'new' });
  });

  it('clip.saved updates an existing row instead of duplicating it', () => {
    let s = reduceEvent(INITIAL_STATE, 'clip.saved', {
      clip_id: 'c2',
      file_path: 'b.mp4',
      trigger_kind: 'chat_hype',
      source: 'event',
    });
    s = reduceEvent(s, 'clip.saved', {
      clip_id: 'c2',
      file_path: 'b.mp4',
      trigger_kind: 'chat_hype',
      source: 'event',
      duration_s: 9,
    });
    expect(s.clips.items).toHaveLength(1);
    expect(s.clips.items[0].durationS).toBe(9);
  });

  it('clip.exported flips an existing clip to status=exported without inventing one', () => {
    let s = applyClipList(INITIAL_STATE, {
      clips: [{ id: 'c1', source: 'event', trigger_kind: 'rl.goal', file_path: 'a.mp4' }],
    });
    s = reduceEvent(s, 'clip.exported', { clip_id: 'c1', export_path: '/export/a.mp4' });
    expect(s.clips.items[0].status).toBe('exported');
    // unknown clip id: no row invented
    const unchanged = reduceEvent(s, 'clip.exported', { clip_id: 'missing', export_path: 'x' });
    expect(unchanged.clips.items).toHaveLength(1);
  });
});
