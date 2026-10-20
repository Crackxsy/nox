/**
 * Who the dashboard is to the core: the pairing link, the stored device credentials, redeeming a
 * code - and the WebSocket address a device uses.
 */

import { describe, expect, it, vi } from 'vitest';

import { resolveWsUrl } from '../../../shared/ipc';
import {
  DEVICE_KEY,
  forgetDevice,
  guessDeviceName,
  loadDevice,
  normaliseCode,
  pairCodeFromHash,
  redeemPairCode,
  saveDevice,
  takePairCode,
} from '../auth';

function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k: string) => data.get(k) ?? null,
    key: (i: number) => [...data.keys()][i] ?? null,
    removeItem: (k: string) => void data.delete(k),
    setItem: (k: string, v: string) => void data.set(k, v),
  };
}

const CREDS = { token: 'x'.repeat(43), clientId: 'device:abc', deviceId: 'abc', name: 'Tablet' };

describe('the pairing link', () => {
  it('takes the code out of the address bar and keeps everything else', () => {
    const replaced: string[] = [];
    const code = takePairCode(
      { hash: '#pair=abcd-efgh&lang=de', pathname: '/dashboard/', search: '?x=1' },
      { replaceState: (_d, _u, url) => replaced.push(String(url)) },
    );
    expect(code).toBe('ABCDEFGH');
    expect(replaced).toEqual(['/dashboard/?x=1#lang=de']);
  });

  it('removes even a malformed code from the address bar, and reports none', () => {
    const replaced: string[] = [];
    const code = takePairCode(
      { hash: '#pair=<script>', pathname: '/dashboard/', search: '' },
      { replaceState: (_d, _u, url) => replaced.push(String(url)) },
    );
    expect(code).toBeNull();
    expect(replaced).toEqual(['/dashboard/']);
  });

  it('treats a link with a broken escape as no code instead of crashing the page', () => {
    expect(() => pairCodeFromHash('#pair=%E0%A4%A')).not.toThrow();
    expect(pairCodeFromHash('#pair=%E0%A4%A')).toBeNull();
    const replaced: string[] = [];
    expect(
      takePairCode(
        { hash: '#pair=%E0%A4%A', pathname: '/dashboard/', search: '' },
        { replaceState: (_d, _u, url) => replaced.push(String(url)) },
      ),
    ).toBeNull();
    expect(replaced).toEqual(['/dashboard/']);
  });

  it('accepts only the core alphabet, in any case, with or without spaces', () => {
    expect(normaliseCode('abcd efgh')).toBe('ABCDEFGH');
    expect(normaliseCode('ABCD0FGH')).toBeNull(); // 0 is not in the alphabet
    expect(normaliseCode('ABCDEFG')).toBeNull();
  });
});

describe('stored device credentials', () => {
  it('round-trips and can be forgotten', () => {
    const store = memoryStorage();
    expect(saveDevice(CREDS, store)).toBe(true);
    expect(loadDevice(store)).toEqual(CREDS);
    forgetDevice(store);
    expect(loadDevice(store)).toBeNull();
  });

  it('refuses credentials whose client id is not their own device', () => {
    const store = memoryStorage();
    store.setItem(DEVICE_KEY, JSON.stringify({ ...CREDS, clientId: 'dashboard:x' }));
    expect(loadDevice(store)).toBeNull();
  });

  it('says so when this browser stores nothing', () => {
    const broken = { ...memoryStorage(), setItem: () => { throw new Error('quota'); } } as Storage;
    expect(saveDevice(CREDS, broken)).toBe(false);
    expect(saveDevice(CREDS, null)).toBe(false);
  });
});

describe('redeeming a code', () => {
  const answer = (status: number, body: unknown) =>
    vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status }));

  it('returns the credentials on success, and posts the code, the typed name and its own guess', async () => {
    const fetchImpl = answer(201, { device_id: 'abc', client_id: 'device:abc', name: 'Tablet', token: CREDS.token });
    const outcome = await redeemPairCode('ABCDEFGH', '  Tablet ', 'iPad', fetchImpl);
    expect(outcome).toEqual({ ok: true, creds: CREDS });
    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe('/api/pair');
    expect(JSON.parse(String(init.body))).toEqual({ code: 'ABCDEFGH', name: 'Tablet', default_name: 'iPad' });
    expect(init.credentials).toBe('omit');
  });

  it('sends an empty name when nothing was typed, so the name from the PC can win', async () => {
    const fetchImpl = answer(201, { device_id: 'abc', client_id: 'device:abc', name: 'Tablet', token: CREDS.token });
    await redeemPairCode('ABCDEFGH', '   ', 'iPhone', fetchImpl);
    const [, init] = fetchImpl.mock.calls[0]!;
    expect(JSON.parse(String(init.body))).toEqual({ code: 'ABCDEFGH', name: '', default_name: 'iPhone' });
  });

  it.each([
    [401, 'used'],
    [401, 'expired'],
    [409, 'max_devices'],
    [429, 'rate_limited'],
    [403, 'origin'],
  ])('maps %i %s to its own message', async (status, error) => {
    expect(await redeemPairCode('ABCDEFGH', '', '', answer(status, { error }))).toEqual({ ok: false, error });
  });

  it('never passes on an unknown reason or a broken success', async () => {
    expect(await redeemPairCode('ABCDEFGH', '', '', answer(500, { error: 'boom' }))).toEqual({ ok: false, error: 'invalid' });
    expect(await redeemPairCode('ABCDEFGH', '', '', answer(201, { token: 'short' }))).toEqual({ ok: false, error: 'invalid' });
  });

  it('reports a network failure as such', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    expect(await redeemPairCode('ABCDEFGH', '', '', fetchImpl)).toEqual({ ok: false, error: 'network' });
  });
});

describe('the WebSocket address', () => {
  it('is the page origin itself when served over HTTPS (the network listener)', async () => {
    const fetchImpl = vi.fn();
    const url = await resolveWsUrl(
      { hostname: 'nox.local', host: 'nox.local:47843', protocol: 'https:', search: '?ws=1', origin: 'https://nox.local:47843' },
      fetchImpl,
    );
    expect(url).toBe('wss://nox.local:47843/ws');
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe('a default device name', () => {
  it('names the product, never a person', () => {
    expect(guessDeviceName('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)')).toBe('iPhone');
    expect(guessDeviceName('Mozilla/5.0 (Linux; Android 15; Pixel) Mobile')).toBe('Android');
    expect(guessDeviceName('curl/8')).toBe('');
  });

  it('recognises an iPad that asks for desktop pages by its touch screen', () => {
    const desktopSafari = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15';
    expect(guessDeviceName(desktopSafari, 5)).toBe('iPad');
    expect(guessDeviceName(desktopSafari, 0)).toBe('Mac');
  });
});
