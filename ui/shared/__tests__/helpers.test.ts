/**
 * The four shared helpers: guards, formatting, failure classification, and the IPC client's idle
 * timeout.
 *
 * The timeout test is the point of this file. Every `chat.send` used to die at ten seconds because
 * the shared request timer was only cleared on the *final* frame, so an answer that streamed for
 * longer was rejected as a timeout and the authoritative response was dropped on the floor.
 */

import { describe, expect, it, vi } from 'vitest';

import { makeEnvelope, parseEnvelope } from '../envelope';
import { errorText, failureKind, reasonText } from '../errors';
import { DASH, formatDuration, formatLatency, formatTimestamp } from '../format';
import { bool, isRecord, num, numOrNull, str, strOrNull, stringList } from '../guards';
import { IpcClient, IpcError, type WebSocketLike, resolveWsUrl } from '../ipc';

describe('guards', () => {
  it('narrows records but not arrays or null', () => {
    expect(isRecord({ a: 1 })).toBe(true);
    expect(isRecord([1])).toBe(false);
    expect(isRecord(null)).toBe(false);
  });

  it('keeps "absent" and "empty" apart', () => {
    expect(str(undefined)).toBe('');
    expect(str(undefined, 'x')).toBe('x');
    expect(strOrNull(undefined)).toBeNull();
    expect(strOrNull('')).toBe('');
    expect(numOrNull(Number.NaN)).toBeNull();
    expect(numOrNull(0)).toBe(0);
    expect(num(undefined, 7)).toBe(7);
    expect(bool('true')).toBe(false);
    expect(stringList(['a', 1, 'b'])).toEqual(['a', 'b']);
  });
});

describe('formatting', () => {
  // Built from one instant so the assertions do not depend on the machine's time zone.
  const stamp = '2026-09-16T20:18:07.488120Z';
  const instant = new Date(stamp);
  const now = new Date(instant.getTime() + 30 * 60 * 1000);
  const twoDaysEarlier = new Date(instant.getTime() - 2 * 24 * 60 * 60 * 1000);

  it('drops the date for today and keeps it otherwise', () => {
    expect(formatTimestamp(stamp, 'de', now)).toMatch(/^heute, \d{2}:\d{2} Uhr$/);
    expect(formatTimestamp(twoDaysEarlier.toISOString(), 'de', now)).toMatch(
      /^\d{2}\.\d{2}\.2026, \d{2}:\d{2} Uhr$/,
    );
  });

  it('never invents a date it could not parse', () => {
    expect(formatTimestamp('not a date', 'de', now)).toBe('not a date');
    expect(formatTimestamp('', 'de', now)).toBe(DASH);
    expect(formatTimestamp(null, 'de', now)).toBe(DASH);
  });

  it('writes durations and latencies the way German writes them', () => {
    expect(formatDuration(12.5, 'de')).toBe('12,5 s');
    expect(formatDuration(12.5, 'en')).toBe('12.5 s');
    expect(formatDuration(95, 'de')).toBe('1:35 min');
    expect(formatLatency(2498, 'de')).toBe('2,5 s');
    expect(formatLatency(120, 'de')).toBe('120 ms');
    expect(formatLatency(null, 'de')).toBe(DASH);
  });
});

describe('failures', () => {
  it('classifies the four cases the UI has different states for', () => {
    expect(failureKind(new IpcError('not_found', 'unknown request'))).toBe('unknown_request');
    expect(failureKind(new IpcError('permission.denied', 'nope'))).toBe('refused');
    expect(failureKind(new IpcError('unavailable', 'off'))).toBe('unavailable');
    expect(failureKind(new IpcError('timeout', 'slow'))).toBe('timeout');
    expect(failureKind(new Error('boom'))).toBe('failed');
  });

  it('keeps the core wording and never loses it', () => {
    expect(reasonText(new Error('boom'))).toBe('boom');
    expect(errorText('Fehler', new Error('boom'))).toBe('Fehler: boom');
    expect(errorText('Fehler', {})).toBe('Fehler');
  });
});

/** A `WebSocketLike` that records what was sent and lets a test answer by hand. */
function fakeSocket() {
  const sent: string[] = [];
  const socket: WebSocketLike = {
    readyState: 1,
    onopen: null,
    onmessage: null,
    onclose: null,
    onerror: null,
    send: (data: string) => sent.push(data),
    close: () => undefined,
  };
  return { socket, sent };
}

function connected() {
  const { socket, sent } = fakeSocket();
  const client = new IpcClient({
    url: 'ws://x/ws',
    token: 't',
    role: 'dashboard',
    id: 'test',
    patterns: [],
    createSocket: () => socket,
    requestTimeoutMs: 10_000,
  });
  client.connect();
  socket.onopen?.({});
  // Answer the handshake so the client counts as online.
  const auth = JSON.parse(sent[0]) as { id: string };
  socket.onmessage?.({
    data: JSON.stringify(
      makeEnvelope('response', 'ipc.auth', { role: 'core', id: 'core' }, { ok: true }, auth.id),
    ),
  });
  return { socket, sent, client };
}

function lastRequestId(sent: string[]): string {
  const env = parseEnvelope(sent[sent.length - 1]);
  expect(env).not.toBeNull();
  return env?.id ?? '';
}

describe('the request timeout is an idle timeout', () => {
  it('a long stream does not time out as long as frames keep arriving', async () => {
    vi.useFakeTimers();
    try {
      const { socket, sent, client } = connected();
      const answer = client.request('chat.send', { text: 'hallo' }, () => undefined);
      const id = lastRequestId(sent);

      // Nine seconds of silence, then a frame; repeat past the 10 s budget several times over.
      for (let i = 0; i < 5; i++) {
        vi.advanceTimersByTime(9000);
        socket.onmessage?.({
          data: JSON.stringify(
            makeEnvelope(
              'stream',
              'chat.send',
              { role: 'core', id: 'core' },
              { delta: 'x', done: false },
              id,
            ),
          ),
        });
      }
      socket.onmessage?.({
        data: JSON.stringify(
          makeEnvelope(
            'stream',
            'chat.send',
            { role: 'core', id: 'core' },
            { text: 'fertig', done: true },
            id,
          ),
        ),
      });
      await expect(answer).resolves.toMatchObject({ text: 'fertig' });
    } finally {
      vi.useRealTimers();
    }
  });

  it('silence still times out', async () => {
    vi.useFakeTimers();
    try {
      const { client } = connected();
      const answer = client.request('chat.send', { text: 'hallo' });
      const rejected = expect(answer).rejects.toMatchObject({ code: 'timeout' });
      vi.advanceTimersByTime(10_001);
      await rejected;
    } finally {
      vi.useRealTimers();
    }
  });

  it('a per-request timeout overrides the client default', async () => {
    vi.useFakeTimers();
    try {
      const { client } = connected();
      const answer = client.request('chat.send', {}, undefined, 60_000);
      const rejected = expect(answer).rejects.toMatchObject({ code: 'timeout' });
      vi.advanceTimersByTime(30_000);
      vi.advanceTimersByTime(30_001);
      await rejected;
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('resolveWsUrl', () => {
  it('prefers an explicit ?ws= port', async () => {
    const url = await resolveWsUrl({ hostname: '127.0.0.1', search: '?ws=1234', origin: 'http://x' });
    expect(url).toBe('ws://127.0.0.1:1234/ws');
  });

  it('falls back to the default port when /health never answers', async () => {
    vi.useFakeTimers();
    try {
      // A core that accepts the connection and never answers. The real `fetch` rejects when its
      // signal aborts; this stand-in has to do the same or the test would prove nothing.
      const hang: typeof fetch = (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new Error('aborted')));
        });
      const pending = resolveWsUrl(
        { hostname: '127.0.0.1', search: '', origin: 'http://127.0.0.1:1' },
        hang,
        47800,
        2000,
      );
      await vi.advanceTimersByTimeAsync(2100);
      await expect(pending).resolves.toBe('ws://127.0.0.1:47800/ws');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('envelope timestamps', () => {
  it('reads a numeric ts as epoch seconds', () => {
    const raw = JSON.stringify({
      v: 1,
      id: 'a',
      ts: 1_789_000_000,
      kind: 'event',
      name: 'system.started',
      corr: null,
      src: { role: 'core', id: 'core' },
      payload: {},
    });
    expect(parseEnvelope(raw)?.ts).toBe(new Date(1_789_000_000_000).toISOString());
  });

  it('refuses a millisecond timestamp rather than rendering the year 57000', () => {
    const raw = JSON.stringify({
      v: 1,
      id: 'a',
      ts: 1_789_000_000_000,
      kind: 'event',
      name: 'system.started',
      corr: null,
      src: { role: 'core', id: 'core' },
      payload: {},
    });
    expect(parseEnvelope(raw)?.ts).toBe('');
  });
});
