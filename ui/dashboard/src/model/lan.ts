/**
 * Devices in the home network (`nox.lan`) and permission prompts, as plain data.
 *
 * Every parser returns `null` for a shape it does not recognise rather than inventing defaults.
 * The Devices page shows that as its own state ("could not read Nox's answer"), never as an empty
 * list that looks like "no devices" and never as "network access is off" - only a core that does
 * not know the request at all (`not_found`) is treated as "off".
 */

export interface LanStatus {
  enabled: boolean;
  running: boolean;
  /** Why the listener is not running. On a paired device only "unavailable" or ''. */
  error: string;
  /** Serving, but the last certificate check failed (local dashboard only). */
  warning: string;
  port: number | null;
  urls: string[];
  /** SHA-256 of the server certificate: what a browser's certificate view shows. */
  fingerprint: string;
  /** SHA-256 of Nox's own certificate authority: to check before installing it. */
  caFingerprint: string;
  caUrl: string;
  /** A security PIN is set and required (for a paired device: to pair, remove others, allow). */
  pinSet: boolean;
}

export interface LanDevice {
  id: string;
  name: string;
  pairedAt: string;
  lastSeenAt: string | null;
  revokedAt: string | null;
  current: boolean;
}

/**
 * A device unused this long is marked in the list. The usual cause is an iPhone paired in Safari
 * and then added to the home screen: the app has storage of its own and pairs again, so the Safari
 * pairing is never seen again.
 */
export const IDLE_AFTER_DAYS = 14;
const DAY_MS = 86_400_000;

/** Whole days since an active device was last seen (or paired, if never), once that reaches
 * `IDLE_AFTER_DAYS`; null for a recent, removed or the asking device, or an unreadable date. */
export function idleDays(device: LanDevice, now: number): number | null {
  if (device.revokedAt !== null || device.current) return null;
  const last = Date.parse(device.lastSeenAt ?? device.pairedAt);
  if (Number.isNaN(last)) return null;
  const days = Math.floor((now - last) / DAY_MS);
  return days >= IDLE_AFTER_DAYS ? days : null;
}

export interface LanPairing {
  code: string;
  expiresAt: string;
  url: string;
  urls: string[];
  qr: string;
  /** Server certificate fingerprint, as in `LanStatus.fingerprint`. */
  fingerprint: string;
  caFingerprint: string;
}

function str(value: unknown): value is string {
  return typeof value === 'string';
}

function strings(value: unknown): string[] | null {
  return Array.isArray(value) && value.every(str) ? value : null;
}

/** The certificate download, only ever a same-origin path (it becomes an `href`). */
const DEFAULT_CA_URL = '/nox-ca.crt';

export function parseLanStatus(payload: unknown): LanStatus | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const p = payload as Record<string, unknown>;
  const urls = strings(p.urls);
  if (typeof p.enabled !== 'boolean' || typeof p.running !== 'boolean' || urls === null) return null;
  const caUrl = str(p.ca_url) && /^\/[\w./-]*$/.test(p.ca_url) && !p.ca_url.startsWith('//') ? p.ca_url : DEFAULT_CA_URL;
  return {
    enabled: p.enabled,
    running: p.running,
    error: str(p.error) ? p.error : '',
    warning: str(p.warning) ? p.warning : '',
    port: typeof p.port === 'number' && Number.isInteger(p.port) && p.port > 0 ? p.port : null,
    urls,
    fingerprint: str(p.fingerprint) ? p.fingerprint : '',
    caFingerprint: str(p.ca_fingerprint) ? p.ca_fingerprint : '',
    caUrl,
    pinSet: p.pin_set === true,
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
  // Active devices first, this one at the very top; removed ones last, newest first.
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
  return {
    code: p.code,
    expiresAt: p.expires_at,
    url: p.url,
    urls,
    qr,
    fingerprint: p.fingerprint,
    caFingerprint: str(p.ca_fingerprint) ? p.ca_fingerprint : '',
  };
}

/** "ABCD EFGH": easier to read off a screen and type into another one. */
export function groupCode(code: string): string {
  return code.length === 8 ? `${code.slice(0, 4)} ${code.slice(4)}` : code;
}

/** "A B C D E F G H": a screen reader spells the code instead of reading it as a word. */
export function spellCode(code: string): string {
  return [...code.replace(/\s/g, '')].join(' ');
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
  /** When no answer becomes "no", in this browser's clock (epoch ms); null when not known. */
  deadline: number | null;
  /** How long a remembered "allow" lasts, in seconds; 0 when the core did not say. */
  rememberTtlS: number;
}

/**
 * `skewMs` is how far the core's clock is ahead of this one (from envelope timestamps), so a phone
 * whose clock is off still counts down to the moment the core gives up.
 */
export function parsePermissionRequested(payload: unknown, skewMs = 0): PermissionPrompt | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const p = payload as Record<string, unknown>;
  if (!str(p.request_id) || !str(p.tool) || !str(p.action)) return null;
  const expires = str(p.expires_at) ? Date.parse(p.expires_at) : Number.NaN;
  const ttl = typeof p.remember_ttl_s === 'number' && Number.isFinite(p.remember_ttl_s) ? p.remember_ttl_s : 0;
  return {
    id: p.request_id,
    agent: str(p.agent) ? p.agent : '',
    tool: p.tool,
    action: p.action,
    target: str(p.target) ? p.target : '',
    risk: str(p.risk) ? p.risk : '',
    deadline: Number.isFinite(expires) ? expires - skewMs : null,
    rememberTtlS: ttl > 0 ? ttl : 0,
  };
}

/**
 * `security.permission.pending {pending: [...]}`, the questions open right now. A malformed entry
 * is skipped (it could not be shown anyway); a malformed answer is `null`.
 */
export function parsePendingPrompts(payload: unknown, skewMs = 0): PermissionPrompt[] | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const list = (payload as Record<string, unknown>).pending;
  if (!Array.isArray(list)) return null;
  let queue: PermissionPrompt[] = [];
  for (const item of list) {
    const prompt = parsePermissionRequested(item, skewMs);
    if (prompt) queue = addPrompt(queue, prompt);
  }
  return queue;
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

/**
 * How a question this dashboard showed but did not answer ended: nobody answered in time (the core
 * said no on its own), or someone answered on another device.
 */
export type DecidedNote = 'timeout' | 'elsewhere';

export function decidedNote(payload: unknown): DecidedNote {
  if (typeof payload !== 'object' || payload === null) return 'elsewhere';
  const p = payload as Record<string, unknown>;
  return p.by === 'policy' && p.rule_id === 'confirm.timeout' ? 'timeout' : 'elsewhere';
}

/** Seconds left until `deadline`, never below zero; null without a deadline. */
export function secondsLeft(deadline: number | null, now: number): number | null {
  if (deadline === null) return null;
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

/** Allowing at this risk from a paired device needs the security PIN (`nox.security.device_pin`). */
export function needsDevicePin(risk: string): boolean {
  return risk === 'high' || risk === 'critical';
}

/** How far the core's clock is ahead of this one, from an envelope timestamp; 0 if unreadable. */
export function clockSkew(envelopeTs: string, now: number): number {
  const at = Date.parse(envelopeTs);
  return Number.isFinite(at) ? at - now : 0;
}
