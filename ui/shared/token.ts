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

export function tokenFromHash(hash: string): string | null {
  const h = hash.startsWith('#') ? hash.slice(1) : hash;
  for (const part of h.split('&')) {
    const [k, v] = part.split('=');
    if (k === 'token' && v) {
      try {
        return decodeURIComponent(v);
      } catch {
        return null;
      }
    }
  }
  return null;
}

/** Strip only the token from the fragment, keep other fragment params (none today). */
export function hashWithoutToken(hash: string): string {
  const h = hash.startsWith('#') ? hash.slice(1) : hash;
  const rest = h.split('&').filter((p) => p && !p.startsWith('token='));
  return rest.length ? `#${rest.join('&')}` : '';
}

export function takeToken(loc: LocationLike, history: HistoryLike): string | null {
  const token = tokenFromHash(loc.hash);
  if (token !== null) {
    history.replaceState(null, '', `${loc.pathname}${loc.search}${hashWithoutToken(loc.hash)}`);
  }
  return token;
}

export function queryFlag(search: string, key: string): boolean {
  const params = new URLSearchParams(search);
  const v = params.get(key);
  return v === '1' || v === 'true';
}

/** Plain query string value (not a secret): used for non-sensitive dev/display flags like
 * `?variant=` (OP-1) or `?expression=` (still-frame renders), never for tokens. */
export function queryString(search: string, key: string): string | null {
  return new URLSearchParams(search).get(key);
}

export function queryInt(search: string, key: string): number | null {
  const v = new URLSearchParams(search).get(key);
  if (v === null) return null;
  const n = Number.parseInt(v, 10);
  return Number.isFinite(n) && n > 0 && n < 65536 ? n : null;
}
