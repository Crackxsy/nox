/**
 * The network-access view model: what it accepts from the core, what it refuses, and how the
 * permission-prompt queue behaves when several devices look at it.
 */

import { describe, expect, it } from 'vitest';

import {
  addPrompt,
  groupCode,
  parseLanDevices,
  parseLanPairing,
  parseLanStatus,
  parsePermissionRequested,
  removePrompt,
  shortFingerprint,
} from '../model';

const PAIRING = {
  code: 'ABCDEFGH',
  expires_at: '2026-10-18T20:05:00+00:00',
  ttl_s: 300,
  url: 'https://192.168.1.20:47843/dashboard/#pair=ABCDEFGH',
  urls: ['https://192.168.1.20:47843/dashboard/#pair=ABCDEFGH'],
  qr: 'data:image/svg+xml;charset=utf-8,%3Csvg%3E',
  fingerprint: 'AA:BB:CC:DD:EE:FF:00:11:22:33',
};

describe('parseLanStatus', () => {
  it('reads a running listener', () => {
    expect(
      parseLanStatus({ enabled: true, running: true, error: '', urls: ['https://x/'], fingerprint: 'AA' }),
    ).toEqual({ enabled: true, running: true, error: '', urls: ['https://x/'], fingerprint: 'AA' });
  });

  it.each([null, 'off', { enabled: 'yes', running: true, urls: [] }, { enabled: true, running: true }])(
    'refuses %j instead of guessing',
    (payload) => {
      expect(parseLanStatus(payload)).toBeNull();
    },
  );
});

describe('parseLanDevices', () => {
  const row = (over: Record<string, unknown>) => ({
    id: 'a',
    name: 'Tablet',
    paired_at: '2026-10-18T10:00:00+00:00',
    last_seen_at: null,
    revoked_at: null,
    current: false,
    ...over,
  });

  it('puts this device first and removed ones last', () => {
    const list = parseLanDevices({
      devices: [
        row({ id: 'old', revoked_at: '2026-10-18T11:00:00+00:00' }),
        row({ id: 'other', paired_at: '2026-10-18T09:00:00+00:00' }),
        row({ id: 'me', current: true, paired_at: '2026-10-18T08:00:00+00:00' }),
      ],
    });
    expect(list?.map((d) => d.id)).toEqual(['me', 'other', 'old']);
  });

  it('refuses a list with a malformed row rather than dropping it silently', () => {
    expect(parseLanDevices({ devices: [row({}), { id: 3 }] })).toBeNull();
  });
});

describe('parseLanPairing', () => {
  it('reads a pairing', () => {
    expect(parseLanPairing(PAIRING)).toMatchObject({ code: 'ABCDEFGH', qr: PAIRING.qr });
  });

  it('never accepts anything but an SVG data URI as the QR image', () => {
    for (const qr of ['javascript:alert(1)', 'https://evil.example/qr.svg', '<svg onload=x>']) {
      expect(parseLanPairing({ ...PAIRING, qr })?.qr).toBe('');
    }
  });
});

describe('formatting', () => {
  it('groups a code in fours and keeps the comparable part of a fingerprint', () => {
    expect(groupCode('ABCDEFGH')).toBe('ABCD EFGH');
    expect(shortFingerprint('AA:BB:CC:DD:EE:FF:00:11:22:33')).toBe('AA:BB:CC:DD:EE:FF:00:11');
  });
});

describe('the permission prompt queue', () => {
  const requested = (id: string) => ({ request_id: id, agent: 'nox', tool: 'files', action: 'delete', mode: 'normal', risk: 'high', target: '/tmp/x' });

  it('queues prompts once each, in arrival order', () => {
    let queue = addPrompt([], parsePermissionRequested(requested('a'))!);
    queue = addPrompt(queue, parsePermissionRequested(requested('b'))!);
    queue = addPrompt(queue, parsePermissionRequested(requested('a'))!);
    expect(queue.map((p) => p.id)).toEqual(['a', 'b']);
  });

  it('drops a prompt when it was decided, wherever that happened', () => {
    const queue = [parsePermissionRequested(requested('a'))!, parsePermissionRequested(requested('b'))!];
    expect(removePrompt(queue, { request_id: 'a', decision: 'allow' }).map((p) => p.id)).toEqual(['b']);
    expect(removePrompt(queue, { decision: 'allow' })).toBe(queue);
  });

  it('refuses a request without the fields a person needs to decide', () => {
    expect(parsePermissionRequested({ request_id: 'a', tool: 'files' })).toBeNull();
  });
});
