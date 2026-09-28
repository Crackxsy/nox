/**
 * The pure half of the PIN, resume and privacy flows: how answers and refusals are read, and when
 * the kill button is armed again.
 */

import { describe, expect, it } from 'vitest';

import { pinRefusalText, translator } from '../i18n';
import { IpcError } from '../ipc';
import {
  killAfterLevel,
  parsePinStatus,
  parsePrivacySetResult,
  parseResumeResult,
  pinRefusal,
  relaxesPrivacy,
} from '../model';

describe('parsePinStatus', () => {
  it('reads the full status', () => {
    expect(
      parsePinStatus({
        configured: true,
        state: 'valid',
        min_length: 6,
        gate_required: true,
        resume_requires_pin: false,
        locked_until: '2026-09-28T12:00:00Z',
      }),
    ).toEqual({
      state: 'valid',
      configured: true,
      minLength: 6,
      gateRequired: true,
      resumeRequiresPin: false,
      lockedUntil: '2026-09-28T12:00:00Z',
    });
  });

  it('still understands an older core that only says `configured`', () => {
    expect(parsePinStatus({ configured: true })?.state).toBe('valid');
    expect(parsePinStatus({ configured: false })?.state).toBe('not_set');
  });

  it('is null for anything else', () => {
    expect(parsePinStatus({ ok: true })).toBeNull();
    expect(parsePinStatus(null)).toBeNull();
  });
});

describe('pinRefusal', () => {
  it('reads the reason and its facts from the error details, not the English message', () => {
    const e = new IpcError('permission.denied', 'whatever wording', false, {
      reason: 'locked',
      locked_until: '2026-09-28T12:15:00Z',
    });
    expect(pinRefusal(e)).toEqual({
      reason: 'locked',
      remainingAttempts: null,
      lockedUntil: '2026-09-28T12:15:00Z',
      minLength: null,
    });
  });

  it('is null for errors that are not about the PIN', () => {
    expect(pinRefusal(new IpcError('rate_limited', 'slow down'))).toBeNull();
    expect(pinRefusal(new IpcError('permission.denied', 'no', false, { reason: 'other' }))).toBeNull();
    expect(pinRefusal(new Error('socket closed'))).toBeNull();
  });
});

describe('pinRefusalText', () => {
  const de = translator('de');
  const en = translator('en');
  const refusal = (reason: string, extra: Record<string, unknown> = {}) =>
    pinRefusal(new IpcError('permission.denied', '', false, { reason, ...extra }))!;

  it('says how many attempts are left', () => {
    expect(pinRefusalText(de, 'de', refusal('pin_wrong', { remaining_attempts: 2 }))).toContain(
      'Noch 2 Versuche',
    );
    expect(pinRefusalText(en, 'en', refusal('pin_wrong', { remaining_attempts: 2 }))).toContain(
      '2 attempts left',
    );
  });

  it('points a broken entry to the command that repairs it', () => {
    expect(pinRefusalText(de, 'de', refusal('invalid_entry'))).toContain('nox pin set');
  });

  it('names the minimum length', () => {
    expect(pinRefusalText(de, 'de', refusal('too_short', { min_length: 6 }))).toContain('6 Zeichen');
  });
});

describe('parseResumeResult', () => {
  it('keeps what the supervisor answered', () => {
    expect(parseResumeResult({ ok: true, supervisor: 'rearmed' })).toEqual({
      ok: true,
      refusal: null,
      supervisor: 'rearmed',
    });
  });

  it('reads a refusal with its reason', () => {
    const result = parseResumeResult({ ok: false, reason: 'pin_wrong', remaining_attempts: 1 });
    expect(result.ok).toBe(false);
    expect(result.refusal?.reason).toBe('pin_wrong');
    expect(result.refusal?.remainingAttempts).toBe(1);
  });
});

describe('killAfterLevel', () => {
  it('re-arms only when the core leaves safe mode', () => {
    expect(killAfterLevel('sent', 'safe_mode', 'running')).toBe('idle');
    expect(killAfterLevel('sent', 'running', 'safe_mode')).toBe('sent');
    expect(killAfterLevel('sent', 'starting', 'running')).toBe('sent');
    expect(killAfterLevel('sent', 'safe_mode', null)).toBe('sent');
    expect(killAfterLevel('confirm', 'safe_mode', 'running')).toBe('confirm');
  });
});

describe('relaxesPrivacy', () => {
  it('matches the core: only moves toward "everything allowed" relax', () => {
    expect(relaxesPrivacy('offline', 'balanced')).toBe(true);
    expect(relaxesPrivacy('balanced', 'full')).toBe(true);
    expect(relaxesPrivacy('full', 'private')).toBe(false);
    expect(relaxesPrivacy('balanced', 'balanced')).toBe(false);
  });

  it('treats an unknown current mode as the strictest (fail closed)', () => {
    expect(relaxesPrivacy(null, 'private')).toBe(true);
    expect(relaxesPrivacy(null, 'offline')).toBe(false);
  });
});

describe('parsePrivacySetResult', () => {
  it('reads applied and requires_confirmation', () => {
    expect(parsePrivacySetResult({ mode: 'balanced', applied: false, requires_confirmation: true })).toEqual({
      mode: 'balanced',
      applied: false,
      requiresConfirmation: true,
    });
  });
});
