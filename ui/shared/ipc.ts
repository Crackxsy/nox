/**
 * WebSocket IPC client for pet and dashboard (IPC Model: handshake, subscribe, request/response,
 * stream frames). Reconnects with backoff; never retries after `auth.denied`. Token only in memory.
 */

import { type Envelope, type Role, makeEnvelope, parseEnvelope } from './envelope';

export type ConnStatus = 'connecting' | 'online' | 'offline' | 'auth_failed';

export interface WebSocketLike {
  readyState: number;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface IpcClientOptions {
  url: string;
  token: string;
  role: Extract<Role, 'pet' | 'dashboard'>;
  id: string;
  patterns: string[];
  clientVersion?: string;
  onEvent?: (env: Envelope) => void;
  onStatus?: (status: ConnStatus, detail?: string) => void;
  /** Injected in tests; defaults to the global WebSocket. */
  createSocket?: (url: string) => WebSocketLike;
  requestTimeoutMs?: number;
  maxBackoffMs?: number;
}

interface Pending {
  resolve: (payload: Record<string, unknown>) => void;
  reject: (err: Error) => void;
  onStream?: (env: Envelope) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class IpcError extends Error {
  constructor(public code: string, message: string, public retryable = false) {
    super(message);
    this.name = 'IpcError';
  }
}

export class IpcClient {
  private ws: WebSocketLike | null = null;
  private pending = new Map<string, Pending>();
  private closedByUser = false;
  private backoff = 1000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private authId: string | null = null;
  status: ConnStatus = 'offline';
  sessionId: string | null = null;

  constructor(private readonly opts: IpcClientOptions) {}

  private get src() {
    return { role: this.opts.role, id: this.opts.id };
  }

  connect(): void {
    this.closedByUser = false;
    this.open();
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.ws?.close(1000, 'client closing');
    this.ws = null;
    this.failAll(new IpcError('closed', 'client closed'));
    this.setStatus('offline');
  }

  private setStatus(s: ConnStatus, detail?: string): void {
    if (s !== this.status) {
      this.status = s;
      this.opts.onStatus?.(s, detail);
    }
  }

  private open(): void {
    this.setStatus('connecting');
    const create = this.opts.createSocket ?? ((u: string) => new WebSocket(u) as unknown as WebSocketLike);
    let ws: WebSocketLike;
    try {
      ws = create(this.opts.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => this.sendAuth();
    ws.onmessage = (ev) => this.onMessage(typeof ev.data === 'string' ? ev.data : '');
    ws.onerror = () => {
      /* onclose follows */
    };
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      this.failAll(new IpcError('unavailable', 'socket closed', true));
      if (this.status !== 'auth_failed') this.setStatus('offline');
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.closedByUser || this.status === 'auth_failed' || this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, this.backoff);
    this.backoff = Math.min(this.backoff * 2, this.opts.maxBackoffMs ?? 10000);
  }

  private sendAuth(): void {
    const env = makeEnvelope('request', 'ipc.auth', this.src, {
      token: this.opts.token,
      role: this.opts.role,
      id: this.opts.id,
      client_version: this.opts.clientVersion ?? '0.1.0',
    });
    this.authId = env.id;
    this.raw(env);
  }

  private raw(env: Envelope): void {
    if (!this.ws || this.ws.readyState !== 1) throw new IpcError('unavailable', 'not connected', true);
    this.ws.send(JSON.stringify(env));
  }

  private onMessage(text: string): void {
    const env = parseEnvelope(text);
    if (!env) {
      console.warn('ipc: dropped invalid frame');
      return;
    }
    if (this.authId && env.corr === this.authId) {
      this.authId = null;
      if (env.kind === 'response' && env.payload.ok === true) {
        this.sessionId = typeof env.payload.session_id === 'string' ? env.payload.session_id : null;
        this.backoff = 1000;
        this.setStatus('online');
        this.request('ipc.subscribe', { patterns: this.opts.patterns }).catch((e) =>
          console.warn('ipc: subscribe failed', e),
        );
      } else {
        this.setStatus('auth_failed', String(env.payload.message ?? env.payload.reason ?? 'auth denied'));
        this.closedByUser = true;
        this.ws?.close(1000, 'auth failed');
      }
      return;
    }
    if (env.kind === 'event') {
      this.opts.onEvent?.(env);
      return;
    }
    if (!env.corr) return;
    const p = this.pending.get(env.corr);
    if (!p) return;
    if (env.kind === 'stream') {
      p.onStream?.(env);
      if (env.payload.done === true) {
        clearTimeout(p.timer);
        this.pending.delete(env.corr);
        p.resolve(env.payload);
      }
      return;
    }
    clearTimeout(p.timer);
    this.pending.delete(env.corr);
    if (env.kind === 'error') {
      p.reject(
        new IpcError(
          String(env.payload.code ?? 'internal'),
          String(env.payload.message ?? 'error'),
          env.payload.retryable === true,
        ),
      );
    } else {
      p.resolve(env.payload);
    }
  }

  private failAll(err: Error): void {
    for (const [id, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(err);
      this.pending.delete(id);
    }
  }

  /** Send a request; resolves with the response payload (or the final `done` stream frame). */
  request(
    name: string,
    payload: Record<string, unknown> = {},
    onStream?: (env: Envelope) => void,
  ): Promise<Record<string, unknown>> {
    const env = makeEnvelope('request', name, this.src, payload);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(env.id);
        reject(new IpcError('timeout', `${name} timed out`, true));
      }, this.opts.requestTimeoutMs ?? 10000);
      this.pending.set(env.id, { resolve, reject, onStream, timer });
      try {
        this.raw(env);
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(env.id);
        reject(e instanceof Error ? e : new Error(String(e)));
      }
    });
  }
}

/** ws URL: `/health` may publish `ws_port`; else `?ws=` query (non-secret); else config default. */
export async function resolveWsUrl(
  loc: { hostname: string; search: string; origin: string },
  fetchImpl: typeof fetch | null = typeof fetch === 'function' ? fetch : null,
  defaultPort = 47800,
): Promise<string> {
  const host = loc.hostname || '127.0.0.1';
  const q = new URLSearchParams(loc.search).get('ws');
  const qPort = q ? Number.parseInt(q, 10) : NaN;
  if (Number.isFinite(qPort) && qPort > 0) return `ws://${host}:${qPort}/ws`;
  if (fetchImpl && loc.origin.startsWith('http')) {
    try {
      const res = await fetchImpl(`${loc.origin}/health`, { cache: 'no-store' });
      if (res.ok) {
        const data = (await res.json()) as { ws_port?: unknown };
        if (typeof data.ws_port === 'number' && data.ws_port > 0) return `ws://${host}:${data.ws_port}/ws`;
      }
    } catch {
      /* fall through to default */
    }
  }
  return `ws://${host}:${defaultPort}/ws`;
}
