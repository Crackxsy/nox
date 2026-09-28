/**
 * What happens to an open page when the core restarts: the session token it holds is refused, the
 * page says so once, and it does not hammer the core with reconnects that can never succeed.
 * Also: the version it sends is the generated one, and an error frame's `details` reach the page.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Envelope } from '../envelope';
import { NOX_VERSION } from '../generated/version';
import { IpcClient, IpcError, type WebSocketLike } from '../ipc';

class FakeSocket implements WebSocketLike {
  readyState = 0;
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  sent: Envelope[] = [];

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Envelope);
  }

  close(): void {
    this.readyState = 3;
    this.onclose?.({});
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  reply(to: Envelope, kind: string, payload: Record<string, unknown>): void {
    const env = { v: 1, id: `r-${to.id}`, ts: 'x', kind, name: to.name, corr: to.id, src: { role: 'core', id: 'core' }, payload };
    this.onmessage?.({ data: JSON.stringify(env) });
  }
}

function makeClient(statuses: string[]) {
  const sockets: FakeSocket[] = [];
  const client = new IpcClient({
    url: 'ws://x',
    token: 'old-token',
    role: 'dashboard',
    id: 'd',
    patterns: [],
    onStatus: (s) => statuses.push(s),
    createSocket: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
  });
  return { client, sockets };
}

describe('a core restart under an open page', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('ends in session_expired, not auth_failed, and never retries', () => {
    const statuses: string[] = [];
    const { client, sockets } = makeClient(statuses);
    client.connect();
    sockets[0].open();
    sockets[0].reply(sockets[0].sent[0], 'response', { ok: true, session_id: 's1' });
    expect(client.status).toBe('online');

    // The core goes away: the socket closes and the client reconnects with backoff ...
    sockets[0].close();
    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(2);
    sockets[1].open();
    // ... to a new core that no longer knows the old token.
    sockets[1].reply(sockets[1].sent[0], 'error', { code: 'auth.denied', message: 'invalid token' });

    expect(client.status).toBe('session_expired');
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(2); // no retry storm
    expect(statuses).toEqual(['connecting', 'online', 'offline', 'connecting', 'session_expired']);
  });

  it('a token refused on the very first try is auth_failed', () => {
    const statuses: string[] = [];
    const { client, sockets } = makeClient(statuses);
    client.connect();
    sockets[0].open();
    sockets[0].reply(sockets[0].sent[0], 'error', { code: 'auth.denied', message: 'invalid token' });
    expect(client.status).toBe('auth_failed');
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
  });
});

describe('the handshake', () => {
  it('sends the generated package version, never a hand-written one', () => {
    const { client, sockets } = makeClient([]);
    client.connect();
    sockets[0].open();
    expect((sockets[0].sent[0].payload as Record<string, unknown>).client_version).toBe(NOX_VERSION);
  });
});

describe('error frames', () => {
  it('carry their details to the caller', async () => {
    const { client, sockets } = makeClient([]);
    client.connect();
    sockets[0].open();
    sockets[0].reply(sockets[0].sent[0], 'response', { ok: true });
    const pending = client.request('security.pin.set', { pin: 'x' });
    const req = sockets[0].sent[2];
    sockets[0].reply(req, 'error', {
      code: 'permission.denied',
      message: 'pin.set denied: wrong PIN',
      details: { reason: 'pin_wrong', remaining_attempts: 3 },
    });
    const error = await pending.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(IpcError);
    expect((error as IpcError).details).toEqual({ reason: 'pin_wrong', remaining_attempts: 3 });
  });
});
