import { describe, expect, it } from 'vitest';

import { makeEnvelope, matchesPattern, parseEnvelope } from '../envelope';
import { IpcClient, type WebSocketLike } from '../ipc';
import { hashWithoutToken, queryFlag, queryString, takeToken, tokenFromHash } from '../token';

const good = {
  v: 1,
  id: 'a1',
  ts: '2026-09-11T10:00:00Z',
  kind: 'event',
  name: 'pet.state_changed',
  corr: null,
  src: { role: 'core', id: 'core' },
  payload: { functional: 'idle' },
};

describe('parseEnvelope', () => {
  it('accepts a valid v1 envelope', () => {
    const env = parseEnvelope(JSON.stringify(good));
    expect(env?.name).toBe('pet.state_changed');
    expect(env?.payload).toEqual({ functional: 'idle' });
    expect(env?.src.role).toBe('core');
  });
  it('accepts numeric ts and missing payload', () => {
    const env = parseEnvelope(JSON.stringify({ ...good, ts: 1_700_000_000, payload: undefined }));
    expect(env?.payload).toEqual({});
    expect(env?.ts).toContain('2023');
  });
  it('rejects wrong version, kind, name, src, payload and non-JSON', () => {
    expect(parseEnvelope('nope')).toBeNull();
    expect(parseEnvelope('[1]')).toBeNull();
    expect(parseEnvelope(JSON.stringify({ ...good, v: 2 }))).toBeNull();
    expect(parseEnvelope(JSON.stringify({ ...good, kind: 'blob' }))).toBeNull();
    expect(parseEnvelope(JSON.stringify({ ...good, name: 'NoDots' }))).toBeNull();
    expect(parseEnvelope(JSON.stringify({ ...good, name: 'pet' }))).toBeNull();
    expect(parseEnvelope(JSON.stringify({ ...good, src: { role: 'hacker', id: 'x' } }))).toBeNull();
    expect(parseEnvelope(JSON.stringify({ ...good, payload: 'text' }))).toBeNull();
    expect(parseEnvelope(JSON.stringify({ ...good, corr: 5 }))).toBeNull();
  });
  it('makeEnvelope produces a parseable frame', () => {
    const env = makeEnvelope('request', 'pet.interact', { role: 'pet', id: 'pet:1' }, { type: 'click' });
    expect(parseEnvelope(JSON.stringify(env))?.id).toBe(env.id);
  });
  it('matchesPattern handles globs', () => {
    expect(matchesPattern('pet.state_changed', 'pet.*')).toBe(true);
    expect(matchesPattern('privacy.mode_changed', 'pet.*')).toBe(false);
    expect(matchesPattern('x.y', '*')).toBe(true);
    expect(matchesPattern('x.y', 'x.y')).toBe(true);
  });
});

describe('token', () => {
  it('reads the token from the hash only and clears it', () => {
    const calls: string[] = [];
    const history = { replaceState: (_d: unknown, _u: string, url?: string | null) => calls.push(String(url)) };
    const token = takeToken({ hash: '#token=abc%2F123', search: '?overlay=1', pathname: '/pet/' }, history);
    expect(token).toBe('abc/123');
    expect(calls).toEqual(['/pet/?overlay=1']);
  });
  it('ignores tokens in the query string', () => {
    const history = { replaceState: () => undefined };
    expect(takeToken({ hash: '', search: '?token=abc', pathname: '/pet/' }, history)).toBeNull();
  });
  it('helpers', () => {
    expect(tokenFromHash('#a=1&token=t&b=2')).toBe('t');
    expect(hashWithoutToken('#a=1&token=t&b=2')).toBe('#a=1&b=2');
    expect(hashWithoutToken('#token=t')).toBe('');
    expect(queryFlag('?overlay=1', 'overlay')).toBe(true);
    expect(queryFlag('?overlay=0', 'overlay')).toBe(false);
    expect(queryString('?variant=fox&still=1', 'variant')).toBe('fox');
    expect(queryString('?variant=fox', 'expression')).toBeNull();
    expect(queryString('', 'variant')).toBeNull();
  });
});

class FakeSocket implements WebSocketLike {
  readyState = 0;
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  sent: Record<string, unknown>[] = [];
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = 3;
    this.onclose?.({});
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  receive(obj: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  reply(to: Record<string, unknown>, kind: 'response' | 'error' | 'stream', payload: Record<string, unknown>) {
    this.receive({ v: 1, id: 'r' + Math.random(), ts: 'x', kind, name: String(to.name), corr: to.id, src: { role: 'core', id: 'core' }, payload });
  }
}

describe('IpcClient handshake', () => {
  it('authenticates with role and token, subscribes, delivers events and answers requests', async () => {
    const sockets: FakeSocket[] = [];
    const events: string[] = [];
    const statuses: string[] = [];
    const client = new IpcClient({
      url: 'ws://127.0.0.1:47800/ws',
      token: 'tok-1234567890123456',
      role: 'pet',
      id: 'pet:test',
      patterns: ['pet.*', 'privacy.*'],
      onEvent: (e) => events.push(e.name),
      onStatus: (s) => statuses.push(s),
      createSocket: () => {
        const s = new FakeSocket();
        sockets.push(s);
        return s;
      },
    });
    client.connect();
    const s = sockets[0];
    s.open();
    const auth = s.sent[0];
    expect(auth.name).toBe('ipc.auth');
    expect(auth.kind).toBe('request');
    expect((auth.payload as Record<string, unknown>).role).toBe('pet');
    expect((auth.payload as Record<string, unknown>).token).toBe('tok-1234567890123456');
    expect(auth.src).toEqual({ role: 'pet', id: 'pet:test' });
    s.reply(auth, 'response', { ok: true, session_id: 's1', core_version: '0.1' });
    expect(client.status).toBe('online');
    expect(client.sessionId).toBe('s1');
    const sub = s.sent[1];
    expect(sub.name).toBe('ipc.subscribe');
    expect((sub.payload as Record<string, unknown>).patterns).toEqual(['pet.*', 'privacy.*']);
    s.reply(sub, 'response', { ok: true });

    s.receive({ v: 1, id: 'e1', ts: 'x', kind: 'event', name: 'pet.state_changed', src: { role: 'core', id: 'core' }, payload: { functional: 'idle' } });
    s.receive({ v: 1, id: 'e2', ts: 'x', kind: 'event', name: 'pet.state_changed', src: { role: 'core', id: 'core' }, payload: 5 });
    expect(events).toEqual(['pet.state_changed']);

    const chunks: string[] = [];
    const p = client.request('chat.send', { text: 'hi' }, (env) => chunks.push(String(env.payload.text)));
    const req = s.sent[2];
    s.reply(req, 'stream', { text: 'a' });
    s.reply(req, 'stream', { text: 'b', done: true });
    expect(await p).toEqual({ text: 'b', done: true });
    expect(chunks).toEqual(['a', 'b']);

    const bad = client.request('mode.set', { mode: 'x' });
    s.reply(s.sent[3], 'error', { code: 'permission.denied', message: 'no' });
    await expect(bad).rejects.toMatchObject({ code: 'permission.denied' });

    s.close();
    expect(client.status).toBe('offline');
    expect(statuses).toEqual(['connecting', 'online', 'offline']);
    client.close();
  });

  it('stops after auth failure and rejects pending requests', async () => {
    const sockets: FakeSocket[] = [];
    const client = new IpcClient({
      url: 'ws://x',
      token: 't',
      role: 'dashboard',
      id: 'd',
      patterns: [],
      createSocket: () => {
        const s = new FakeSocket();
        sockets.push(s);
        return s;
      },
    });
    client.connect();
    sockets[0].open();
    sockets[0].reply(sockets[0].sent[0], 'error', { code: 'auth.denied', message: 'bad token' });
    expect(client.status).toBe('auth_failed');
    await expect(client.request('ipc.ping')).rejects.toMatchObject({ code: 'unavailable' });
    expect(sockets.length).toBe(1);
  });
});
