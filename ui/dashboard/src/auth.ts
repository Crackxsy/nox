/**
 * Who this dashboard is to the core.
 *
 * - **local**: opened on the machine Nox runs on, with the per-start session token from the URL
 *   fragment (`../../shared/token.ts`): kept in memory only, gone with the tab.
 * - **device**: a phone, tablet or PC paired over the network listener (`nox.lan`). Its token is
 *   long-lived by design - the point of pairing is not to pair again tomorrow - so it is kept in
 *   this origin's storage. Storage choice does not change the threat: script running on this
 *   origin could read any of them, which the listener's strict Content-Security-Policy is there to
 *   prevent. What protects the device is that its token only works through the listener, only
 *   for its own device id, only for day-to-day requests, and dies when it is removed.
 *
 * Pairing arrives as `#pair=<code>` (the QR code's link). Like the session token, the code is
 * taken out of the address bar at once, so it never lands in history or a bookmark.
 */

export interface DeviceCredentials {
  token: string;
  clientId: string;
  deviceId: string;
  name: string;
}

export type Auth = { kind: 'local'; token: string } | ({ kind: 'device' } & DeviceCredentials);

export const DEVICE_KEY = 'nox.device.v1';

/** Storage can be missing or throw (private mode, blocked site data); callers get null then. */
function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

function isCredentials(value: unknown): value is DeviceCredentials {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.token === 'string' &&
    v.token.length >= 32 &&
    typeof v.clientId === 'string' &&
    v.clientId === `device:${String(v.deviceId)}` &&
    typeof v.deviceId === 'string' &&
    typeof v.name === 'string'
  );
}

export function loadDevice(store: Storage | null = storage()): DeviceCredentials | null {
  try {
    const raw = store?.getItem(DEVICE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return isCredentials(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** False when nothing could be stored - the page then says the device forgets the pairing. */
export function saveDevice(creds: DeviceCredentials, store: Storage | null = storage()): boolean {
  try {
    if (!store) return false;
    store.setItem(DEVICE_KEY, JSON.stringify(creds));
    return true;
  } catch {
    return false;
  }
}

export function forgetDevice(store: Storage | null = storage()): void {
  try {
    store?.removeItem(DEVICE_KEY);
  } catch {
    /* nothing to forget */
  }
}

/** Crockford-style, as the core issues them (`nox.remote.pairing.CODE_ALPHABET`). */
const CODE_RE = /^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{8}$/;

export function normaliseCode(input: string): string | null {
  const code = input.replace(/[\s-]/g, '').toUpperCase();
  return CODE_RE.test(code) ? code : null;
}

export function pairCodeFromHash(hash: string): string | null {
  const h = hash.startsWith('#') ? hash.slice(1) : hash;
  for (const part of h.split('&')) {
    const [k, v] = part.split('=');
    if (k === 'pair' && v) return normaliseCode(decodeURIComponent(v));
  }
  return null;
}

export function takePairCode(
  loc: { hash: string; pathname: string; search: string },
  history: { replaceState(data: unknown, unused: string, url?: string | null): void },
): string | null {
  const code = pairCodeFromHash(loc.hash);
  if (/(^|[#&])pair=/.test(loc.hash)) {
    const rest = loc.hash
      .replace(/^#/, '')
      .split('&')
      .filter((p) => p && !p.startsWith('pair='));
    history.replaceState(null, '', `${loc.pathname}${loc.search}${rest.length ? `#${rest.join('&')}` : ''}`);
  }
  return code;
}

/** Why a pairing did not work, in the words the page shows (`pair_error_*` keys). */
export type PairError =
  | 'unknown_code'
  | 'used'
  | 'expired'
  | 'max_devices'
  | 'rate_limited'
  | 'origin'
  | 'invalid'
  | 'network';

export type PairOutcome = { ok: true; creds: DeviceCredentials } | { ok: false; error: PairError };

const KNOWN: ReadonlySet<string> = new Set([
  'unknown_code',
  'used',
  'expired',
  'max_devices',
  'rate_limited',
  'origin',
  'invalid',
]);

export async function redeemPairCode(
  code: string,
  name: string,
  fetchImpl: typeof fetch = fetch,
): Promise<PairOutcome> {
  let res: Response;
  try {
    res = await fetchImpl('/api/pair', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, name: name.trim().slice(0, 64) }),
      cache: 'no-store',
      credentials: 'omit',
    });
  } catch {
    return { ok: false, error: 'network' };
  }
  let body: Record<string, unknown> = {};
  try {
    body = (await res.json()) as Record<string, unknown>;
  } catch {
    /* an empty or non-JSON answer is reported by status below */
  }
  if (res.status === 201) {
    const creds = {
      token: body.token,
      clientId: body.client_id,
      deviceId: body.device_id,
      name: body.name,
    };
    return isCredentials(creds) ? { ok: true, creds } : { ok: false, error: 'invalid' };
  }
  const reason = typeof body.error === 'string' && KNOWN.has(body.error) ? (body.error as PairError) : 'invalid';
  return { ok: false, error: reason };
}

/** A neutral default name for this device (a product name, no language), from its user agent. */
export function guessDeviceName(ua: string): string {
  if (/iPad/.test(ua)) return 'iPad';
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/Android/.test(ua)) return 'Android';
  if (/Macintosh/.test(ua)) return 'Mac';
  if (/Windows/.test(ua)) return 'Windows';
  if (/Linux/.test(ua)) return 'Linux';
  return '';
}
