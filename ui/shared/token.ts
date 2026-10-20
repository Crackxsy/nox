/**
 * Session token handling for pet/dashboard (IPC Model handshake step 2, hardened):
 * the token arrives ONCE in the URL fragment (`#token=…`), is kept in memory only and the fragment
 * is removed from the address bar immediately. Fragments never reach HTTP logs or Referer headers.
 * localStorage/sessionStorage are never used for it.
 */

export interface LocationLike {
  hash: string;
  search: string;
  pathname: string;
}

export interface HistoryLike {
  replaceState(data: unknown, unused: string, url?: string | null): void;
}

export function tokenFromHash(hash: string, key = 'token'): string | null {
  const h = hash.startsWith('#') ? hash.slice(1) : hash;
  for (const part of h.split('&')) {
    const [k, v] = part.split('=');
    if (k === key && v) {
      try {
        return decodeURIComponent(v);
      } catch {
        return null;
      }
    }
  }
  return null;
}

/** Strip the credentials (`token=`, `overlay=`) from the fragment, keep other fragment params. */
export function hashWithoutToken(hash: string): string {
  const h = hash.startsWith('#') ? hash.slice(1) : hash;
  const rest = h
    .split('&')
    .filter((p) => p && !p.startsWith('token=') && !p.startsWith('overlay='));
  return rest.length ? `#${rest.join('&')}` : '';
}

export function takeToken(loc: LocationLike, history: HistoryLike): string | null {
  const token = tokenFromHash(loc.hash);
  if (token !== null) {
    history.replaceState(null, '', `${loc.pathname}${loc.search}${hashWithoutToken(loc.hash)}`);
  }
  return token;
}

/** Which credential a page was opened with: the session token (`#token=`, shell-opened windows)
 * or a persistent, read-only overlay credential (`#overlay=`, an OBS browser source). */
export interface Credential {
  token: string;
  role: 'session' | 'overlay';
}

/** Like `takeToken`, for the pet page: an overlay credential wins over a session token (it is the
 * one OBS keeps across restarts). Removed from the address bar either way. */
export function takeCredential(loc: LocationLike, history: HistoryLike): Credential | null {
  const overlay = tokenFromHash(loc.hash, 'overlay');
  const session = tokenFromHash(loc.hash);
  if (overlay === null && session === null) return null;
  history.replaceState(null, '', `${loc.pathname}${loc.search}${hashWithoutToken(loc.hash)}`);
  return overlay !== null ? { token: overlay, role: 'overlay' } : { token: session!, role: 'session' };
}

export function queryFlag(search: string, key: string): boolean {
  const params = new URLSearchParams(search);
  const v = params.get(key);
  return v === '1' || v === 'true';
}

/** Plain query string value (not a secret): used for non-sensitive dev/display flags like
 * `?variant=` or `?expression=` (still-frame renders), never for tokens. */
export function queryString(search: string, key: string): string | null {
  return new URLSearchParams(search).get(key);
}

export function queryInt(search: string, key: string): number | null {
  const v = new URLSearchParams(search).get(key);
  if (v === null) return null;
  const n = Number.parseInt(v, 10);
  return Number.isFinite(n) && n > 0 && n < 65536 ? n : null;
}
