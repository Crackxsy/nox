/**
 * The network-access view model: what it accepts from the core, what it refuses, and how the
 * permission-prompt queue behaves when several devices look at it.
 */

import { describe, expect, it } from 'vitest';

import { ipcReasonLabel, rememberLabel, translator } from '../i18n';
import {
  addPrompt,
  clockSkew,
  decidedNote,
  IDLE_AFTER_DAYS,
  type LanDevice,
  groupCode,
  idleDays,
  needsDevicePin,
  parseLanDevices,
  parseLanPairing,
  parseLanStatus,
  parsePendingPrompts,
  parsePermissionRequested,
  removePrompt,
  secondsLeft,
  shortFingerprint,
  spellCode,
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
  it('reads a running listener, both fingerprints and whether a PIN is set', () => {
    expect(
      parseLanStatus({
        enabled: true,
        running: true,
        error: '',
        warning: '',
        port: 47843,
        urls: ['https://x/'],
        fingerprint: 'AA',
        ca_fingerprint: 'CC',
        ca_url: '/nox-ca.crt',
        pin_set: true,
      }),
    ).toEqual({
      enabled: true,
      running: true,
      error: '',
      warning: '',
      port: 47843,
      urls: ['https://x/'],
      fingerprint: 'AA',
      caFingerprint: 'CC',
      caUrl: '/nox-ca.crt',
      pinSet: true,
    });
  });

  it('defaults what an older core does not send, and never a PIN it did not report', () => {
    const status = parseLanStatus({ enabled: true, running: false, urls: [] });
    expect(status).toMatchObject({ pinSet: false, caFingerprint: '', caUrl: '/nox-ca.crt', port: null });
  });

  it('only ever links the certificate on this origin', () => {
    for (const ca_url of ['https://evil.example/ca.crt', '//evil.example/ca.crt', 'javascript:alert(1)']) {
      expect(parseLanStatus({ enabled: true, running: true, urls: [], ca_url })?.caUrl).toBe('/nox-ca.crt');
    }
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

  it('spells a code for a screen reader', () => {
    expect(spellCode('ABCD EFGH')).toBe('A B C D E F G H');
  });
});

describe('refusal reasons and remembered grants in words', () => {
  const de = translator('de');
  const en = translator('en');

  it('maps every reason the core sends, and nothing it does not', () => {
    for (const reason of ['off', 'starting', 'failed', 'no_address', 'pin_not_set', 'pin_required', 'pin_wrong', 'removed']) {
      expect(ipcReasonLabel(de, reason), reason).toBeTruthy();
      expect(ipcReasonLabel(en, reason), reason).toBeTruthy();
    }
    expect(ipcReasonLabel(de, 'something_new')).toBeNull();
    expect(ipcReasonLabel(de, '')).toBeNull();
  });

  it('says for how long, never "always"', () => {
    expect(rememberLabel(de, 8 * 3600)).toBe('Für 8 Stunden erlauben');
    expect(rememberLabel(en, 8 * 3600)).toBe('Allow for 8 hours');
    expect(rememberLabel(de, 3600)).toBe('Für 1 Stunde erlauben');
    expect(rememberLabel(de, 1800)).toBe('Für 30 Minuten erlauben');
    expect(rememberLabel(de, 5400)).toBe('Für 90 Minuten erlauben');
    expect(rememberLabel(de, 0)).toBeNull();
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

  it('counts down to the core\'s deadline, corrected for a clock that is off', () => {
    const prompt = parsePermissionRequested(
      { ...requested('a'), expires_at: '2026-10-19T10:01:00+00:00', remember_ttl_s: 28800 },
      5000, // the core's clock is 5 s ahead of this one
    )!;
    expect(prompt.deadline).toBe(Date.parse('2026-10-19T10:01:00Z') - 5000);
    expect(prompt.rememberTtlS).toBe(28800);
    expect(secondsLeft(prompt.deadline, Date.parse('2026-10-19T10:00:00Z'))).toBe(55);
    expect(secondsLeft(prompt.deadline, Date.parse('2026-10-19T10:05:00Z'))).toBe(0);
    expect(secondsLeft(parsePermissionRequested(requested('b'))!.deadline, 0)).toBeNull();
    expect(clockSkew('2026-10-19T10:00:05Z', Date.parse('2026-10-19T10:00:00Z'))).toBe(5000);
    expect(clockSkew('', 0)).toBe(0);
  });

  it('reads the questions still open after a reconnect, skipping what it cannot show', () => {
    const queue = parsePendingPrompts({ pending: [requested('a'), { request_id: 'x' }, requested('b'), requested('a')] });
    expect(queue?.map((p) => p.id)).toEqual(['a', 'b']);
    expect(parsePendingPrompts({ pending: [] })).toEqual([]);
    expect(parsePendingPrompts({})).toBeNull();
  });

  it('tells "nobody answered" from "answered on another device"', () => {
    expect(decidedNote({ request_id: 'a', decision: 'deny', rule_id: 'confirm.timeout', by: 'policy' })).toBe('timeout');
    expect(decidedNote({ request_id: 'a', decision: 'deny', rule_id: 'confirm.denied', by: 'device:x' })).toBe('elsewhere');
    expect(decidedNote({ request_id: 'a', decision: 'allow', rule_id: 'confirm.user', by: 'dashboard:y' })).toBe('elsewhere');
  });

  it('asks a paired device for the PIN at high and critical risk only', () => {
    expect(['read', 'low', 'medium', 'high', 'critical'].map(needsDevicePin)).toEqual([false, false, false, true, true]);
  });
});

describe('devices nobody uses any more', () => {
  const NOW = Date.parse('2026-11-30T12:00:00Z');
  const base: LanDevice = {
    id: 'a',
    name: 'iPhone',
    pairedAt: '2026-10-01T12:00:00Z',
    lastSeenAt: null,
    revokedAt: null,
    current: false,
  };
  const daysAgo = (n: number) => new Date(NOW - n * 86_400_000).toISOString();

  it('counts from the last time it was seen, or from pairing when it never was', () => {
    expect(idleDays({ ...base, lastSeenAt: daysAgo(IDLE_AFTER_DAYS) }, NOW)).toBe(IDLE_AFTER_DAYS);
    expect(idleDays({ ...base, lastSeenAt: daysAgo(IDLE_AFTER_DAYS - 1) }, NOW)).toBeNull();
    expect(idleDays({ ...base, pairedAt: daysAgo(40) }, NOW)).toBe(40);
  });

  it('never marks a removed device, the asking one or an unreadable date', () => {
    const old = { ...base, lastSeenAt: daysAgo(90) };
    expect(idleDays({ ...old, revokedAt: daysAgo(1) }, NOW)).toBeNull();
    expect(idleDays({ ...old, current: true }, NOW)).toBeNull();
    expect(idleDays({ ...base, lastSeenAt: 'not a date' }, NOW)).toBeNull();
  });
});
