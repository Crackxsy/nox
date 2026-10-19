/**
 * Devices in the home network (`nox.lan`) and permission prompts, as plain data.
 *
 * Every parser returns `null` for a shape it does not recognise rather than inventing defaults:
 * the page then says it could not read the answer, instead of showing an empty list that looks
 * like "no devices".
 */

export interface LanStatus {
  enabled: boolean;
  running: boolean;
  error: string;
  urls: string[];
  fingerprint: string;
}

export interface LanDevice {
  id: string;
  name: string;
  pairedAt: string;
  lastSeenAt: string | null;
  revokedAt: string | null;
  current: boolean;
}

export interface LanPairing {
  code: string;
  expiresAt: string;
  url: string;
  urls: string[];
  qr: string;
  fingerprint: string;
}

function str(value: unknown): value is string {
  return typeof value === 'string';
}

function strings(value: unknown): string[] | null {
  return Array.isArray(value) && value.every(str) ? value : null;
}

export function parseLanStatus(payload: unknown): LanStatus | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const p = payload as Record<string, unknown>;
  const urls = strings(p.urls);
  if (typeof p.enabled !== 'boolean' || typeof p.running !== 'boolean' || urls === null) return null;
  return {
    enabled: p.enabled,
    running: p.running,
    error: str(p.error) ? p.error : '',
    urls,
    fingerprint: str(p.fingerprint) ? p.fingerprint : '',
  };
}

export function parseLanDevices(payload: unknown): LanDevice[] | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const list = (payload as Record<string, unknown>).devices;
  if (!Array.isArray(list)) return null;
  const out: LanDevice[] = [];
  for (const item of list) {
    if (typeof item !== 'object' || item === null) return null;
    const d = item as Record<string, unknown>;
    if (!str(d.id) || !str(d.name) || !str(d.paired_at)) return null;
    out.push({
      id: d.id,
      name: d.name,
      pairedAt: d.paired_at,
      lastSeenAt: str(d.last_seen_at) ? d.last_seen_at : null,
      revokedAt: str(d.revoked_at) ? d.revoked_at : null,
      current: d.current === true,
    });
  }
  // Active devices first, this one at the very top; revoked ones last, newest first.
  return out.sort((a, b) => {
    if (a.current !== b.current) return a.current ? -1 : 1;
    if ((a.revokedAt === null) !== (b.revokedAt === null)) return a.revokedAt === null ? -1 : 1;
    return b.pairedAt.localeCompare(a.pairedAt);
  });
}

export function parseLanPairing(payload: unknown): LanPairing | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const p = payload as Record<string, unknown>;
  const urls = strings(p.urls);
  if (!str(p.code) || !str(p.expires_at) || !str(p.url) || urls === null || !str(p.fingerprint)) return null;
  // The QR code is shown as an <img>: only an SVG data URI from the core is accepted, never markup.
  const qr = str(p.qr) && p.qr.startsWith('data:image/svg+xml') ? p.qr : '';
  return { code: p.code, expiresAt: p.expires_at, url: p.url, urls, qr, fingerprint: p.fingerprint };
}

/** "ABCD EFGH": easier to read off a screen and type into another one. */
export function groupCode(code: string): string {
  return code.length === 8 ? `${code.slice(0, 4)} ${code.slice(4)}` : code;
}

/** The first eight byte pairs of a fingerprint - what a person can actually compare. */
export function shortFingerprint(fingerprint: string): string {
  return fingerprint.split(':').slice(0, 8).join(':');
}

// ---- permission prompts ---------------------------------------------------------------------

export interface PermissionPrompt {
  id: string;
  agent: string;
  tool: string;
  action: string;
  target: string;
  risk: string;
}

export function parsePermissionRequested(payload: unknown): PermissionPrompt | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const p = payload as Record<string, unknown>;
  if (!str(p.request_id) || !str(p.tool) || !str(p.action)) return null;
  return {
    id: p.request_id,
    agent: str(p.agent) ? p.agent : '',
    tool: p.tool,
    action: p.action,
    target: str(p.target) ? p.target : '',
    risk: str(p.risk) ? p.risk : '',
  };
}

/** Prompts in arrival order; an id already waiting is not added twice. */
export function addPrompt(queue: PermissionPrompt[], prompt: PermissionPrompt): PermissionPrompt[] {
  return queue.some((p) => p.id === prompt.id) ? queue : [...queue, prompt];
}

/** A decision - here or on any other device - takes the prompt away everywhere. */
export function removePrompt(queue: PermissionPrompt[], decidedPayload: unknown): PermissionPrompt[] {
  if (typeof decidedPayload !== 'object' || decidedPayload === null) return queue;
  const id = (decidedPayload as Record<string, unknown>).request_id;
  return str(id) ? queue.filter((p) => p.id !== id) : queue;
}
