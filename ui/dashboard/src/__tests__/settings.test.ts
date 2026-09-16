/**
 * Settings page logic around the PIN (#23).
 *
 * The rule the page has to get right is narrow and worth pinning down: a PIN is sent exactly when
 * the core says one is configured and the user typed one, it is never part of any other request,
 * and a refusal that mentions the PIN turns into the honest message instead of a raw error line.
 */

import { describe, expect, it } from 'vitest';

import { DICT, type Key } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { parseConfigSetResult, parsePinConfigured, pinErrorKey } from '../model';

interface Sent {
  name: string;
  payload: Record<string, unknown>;
}

/** An `IpcClient` that answers nothing and only records what the page asked it to send. */
function recorder(): { client: IpcClient; sent: Sent[] } {
  const sent: Sent[] = [];
  const client = {
    request: (name: string, payload: Record<string, unknown>) => {
      sent.push({ name, payload });
      return Promise.resolve({ ok: true });
    },
  } as unknown as IpcClient;
  return { client, sent };
}

describe('secrets requests carry a PIN only when there is one', () => {
  it('sends no pin field at all when no PIN is configured', async () => {
    const { client, sent } = recorder();
    await api.secretSet(client, 'nox/obs/websocket_password', 'pw');
    await api.secretDelete(client, 'nox/obs/websocket_password');
    expect(sent.map((s) => s.name)).toEqual(['secrets.set', 'secrets.delete']);
    for (const { payload } of sent) expect('pin' in payload).toBe(false);
  });

  it('attaches the typed PIN when one is configured', async () => {
    const { client, sent } = recorder();
    await api.secretSet(client, 'nox/twitch/client_id', 'abc', '4711');
    await api.secretDelete(client, 'nox/twitch/client_id', '4711');
    expect(sent[0].payload).toEqual({ name: 'nox/twitch/client_id', value: 'abc', pin: '4711' });
    expect(sent[1].payload).toEqual({ name: 'nox/twitch/client_id', pin: '4711' });
  });

  it('treats an empty PIN as no PIN, so the core answers "PIN required"', async () => {
    const { client, sent } = recorder();
    await api.secretSet(client, 'nox/obs/websocket_password', 'pw', '');
    expect('pin' in sent[0].payload).toBe(false);
  });

  it('never puts a PIN into an unrelated request', async () => {
    const { client, sent } = recorder();
    await api.configSet(client, { 'identity.name': 'Nox' });
    await api.secretsStatus(client);
    await api.pinStatus(client);
    expect(sent.map((s) => s.name)).toEqual(['config.set', 'secrets.status', 'security.pin.status']);
    expect(JSON.stringify(sent)).not.toContain('pin"');
  });
});

describe('parsePinConfigured', () => {
  it('reads the flag the core sends', () => {
    expect(parsePinConfigured({ configured: true })).toBe(true);
    expect(parsePinConfigured({ configured: false })).toBe(false);
  });
  it('is false for anything it cannot read, so the page asks for nothing it invented', () => {
    expect(parsePinConfigured(null)).toBe(false);
    expect(parsePinConfigured({})).toBe(false);
    expect(parsePinConfigured({ configured: 'yes' })).toBe(false);
    expect(parsePinConfigured('configured')).toBe(false);
  });
});

describe('pinErrorKey', () => {
  const refusal = 'PIN required to change a stored secret';

  it('asks for a PIN when none was sent and names a wrong one when it was', () => {
    expect(pinErrorKey('permission.denied', refusal, false)).toBe('pin_required');
    expect(pinErrorKey('permission.denied', refusal, true)).toBe('pin_wrong');
  });

  it('leaves an unrelated error with its own message', () => {
    expect(pinErrorKey('permission.denied', "unknown secret name 'nox/x'", false)).toBeNull();
    expect(pinErrorKey('rate_limited', 'at most 10 secret changes per 60s', true)).toBeNull();
    expect(pinErrorKey('validation.failed', 'value must not be empty', true)).toBeNull();
  });

  it('has a message in both languages for every key it can return', () => {
    for (const key of ['pin_required', 'pin_wrong', 'pin_label', 'pin_hint'] as Key[]) {
      expect(DICT.de[key]).toBeTruthy();
      expect(DICT.en[key]).toBeTruthy();
    }
  });
});

describe('config.set answers', () => {
  it('an ok answer without an applied list is still an ok answer', () => {
    // The core returns `applied: []` whenever every accepted path is restart-required — the
    // values are written to user.yaml, they just do not take effect in the running core. The page
    // must not read that as "nothing was saved" and keep the form dirty forever.
    const parsed = parseConfigSetResult({
      ok: true,
      applied: [],
      restart_required: ['plugins.enabled'],
      errors: {},
    });
    expect(parsed.ok).toBe(true);
    expect(parsed.applied).toEqual([]);
    expect(parsed.restartRequired).toEqual(['plugins.enabled']);
  });

  it('is not ok when the core reported a per-path error', () => {
    const parsed = parseConfigSetResult({
      ok: true,
      applied: ['identity.name'],
      errors: { 'stream.twitch.rate_limit_max_messages': 'Input should be less than or equal to 100' },
    });
    expect(parsed.ok).toBe(false);
    expect(parsed.errors['stream.twitch.rate_limit_max_messages']).toContain('100');
  });
});

describe('labels for the settings the core reports', () => {
  it('names every Twitch bot setting in both languages (#26)', () => {
    for (const key of [
      'setting_stream_twitch_channel',
      'setting_stream_twitch_bot_names',
      'setting_stream_twitch_relevance_cooldown_s',
      'setting_stream_twitch_rate_limit_max_messages',
      'setting_stream_twitch_rate_limit_window_s',
      'setting_stream_twitch_rate_limit_min_gap_s',
      'setting_stream_twitch_min_backoff_s',
      'setting_stream_twitch_max_backoff_s',
    ] as Key[]) {
      expect(DICT.de[key], `de.${key}`).toBeTruthy();
      expect(DICT.en[key], `en.${key}`).toBeTruthy();
    }
  });
});
