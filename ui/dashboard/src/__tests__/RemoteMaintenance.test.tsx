/**
 * Einstellungen › System › Fernwartung (SSH): shown only on Nox-OS; keys first (PIN and maintenance
 * passphrase), shown by their fingerprints; on only after the owner confirmed exactly those (PIN,
 * passphrase, time); the passphrase set during setup and changed with the old one; a visible notice
 * while on; off and forgetting with one click; refusals in words - against a scripted core.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { RemoteMaintenance, parseSshStatus } from '../areas/settings/RemoteMaintenance';
import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';

const t = translator('de');
/** A fictitious ed25519 public key, built here from the bytes 0..31 (nobody owns it). */
function publicKey(): string {
  const field = (bytes: number[]) => [0, 0, 0, bytes.length, ...bytes];
  const kind = Array.from('ssh-ed25519', (c) => c.charCodeAt(0));
  const blob = [...field(kind), ...field(Array.from({ length: 32 }, (_, i) => i))];
  return `ssh-ed25519 ${btoa(String.fromCharCode(...blob))} admin@example.invalid`;
}
const KEY = publicKey();
const FP = 'SHA256:fictitiousFingerprintOfAKeyNobodyOwns000000';
const STORED = [{ type: 'ssh-ed25519', fingerprint: FP, comment: 'laptop' }];
const PASS = 'correct horse battery staple'; // fictitious

function status(over: Record<string, unknown> = {}) {
  return {
    available: true,
    reason: '',
    on: false,
    until: null,
    seconds_left: null,
    active_keys: [],
    keys: [],
    passphrase_set: true,
    locked_until: null,
    setup_mode: false,
    default_minutes: 60,
    min_minutes: 5,
    max_minutes: 240,
    ...over,
  };
}

const ON = status({
  on: true,
  until: '2026-10-22T13:00:00+00:00',
  seconds_left: 3600,
  active_keys: [FP],
  keys: STORED,
});

type Answers = Record<string, unknown>;

function core(over: Answers = {}) {
  const calls: { name: string; payload: Record<string, unknown> }[] = [];
  const answers: Answers = {
    'noxos.ssh.status': status(),
    'security.pin.status': { configured: true },
    'noxos.ssh.keys.set': status({ keys: STORED }),
    'noxos.ssh.enable': ON,
    'noxos.ssh.disable': status({ keys: STORED }),
    'noxos.ssh.passphrase.set': status(),
    'noxos.ssh.passphrase.change': status(),
    ...over,
  };
  const client = {
    request: vi.fn((name: string, payload: Record<string, unknown>) => {
      calls.push({ name, payload });
      const answer = answers[name];
      if (answer instanceof Error) return Promise.reject(answer);
      return Promise.resolve(answer);
    }),
  } as unknown as IpcClient;
  return { client, calls };
}

async function show(over: Answers = {}) {
  const scripted = core(over);
  await act(async () => {
    render(<RemoteMaintenance t={t} lang="de" client={scripted.client} />);
  });
  return scripted;
}

async function click(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name }));
  });
}

/** The PIN field of the main form (the passphrase form below has its own). */
const mainPin = () => screen.getAllByLabelText(t('lan_pair_pin'))[0] as HTMLInputElement;
const type = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });

describe('RemoteMaintenance', () => {
  it('shows nothing where it is not Nox-OS', async () => {
    await show({ 'noxos.ssh.status': status({ available: false, reason: 'not_nox_os' }) });
    expect(screen.queryByText(t('ssh_title'))).toBeNull();
    expect(parseSshStatus({ available: false })).toBeNull();
  });

  it('shows nothing for an older core without the request', async () => {
    await show({ 'noxos.ssh.status': new IpcError('not_found', "unknown request 'noxos.ssh.status'") });
    expect(screen.queryByText(t('ssh_title'))).toBeNull();
  });

  it('stores keys first, with PIN and passphrase, and shows their fingerprints', async () => {
    const { calls } = await show();
    const save = screen.getByRole('button', { name: t('ssh_keys_save') }) as HTMLButtonElement;
    type(screen.getByLabelText(t('ssh_keys_label')), KEY);
    type(mainPin(), '4711');
    expect(save.disabled).toBe(true); // no passphrase yet
    const secret = screen.getByLabelText(t('ssh_passphrase')) as HTMLInputElement;
    type(secret, PASS);
    await click(t('ssh_keys_save'));
    expect(calls.find((c) => c.name === 'noxos.ssh.keys.set')?.payload).toEqual({
      keys: KEY,
      passphrase: PASS,
      pin: '4711',
    });
    expect(calls.some((c) => c.name === 'noxos.ssh.enable')).toBe(false);
    expect(screen.getByText(FP)).toBeTruthy();
    expect(screen.getByText(t('ssh_off_state'))).toBeTruthy();
  });

  it('switches on only after the fingerprints are confirmed, naming exactly those', async () => {
    const { calls } = await show({ 'noxos.ssh.status': status({ keys: STORED }) });
    const on = screen.getByRole('button', { name: t('ssh_on') }) as HTMLButtonElement;
    type(mainPin(), '4711');
    type(screen.getByLabelText(t('ssh_passphrase')), PASS);
    type(screen.getByLabelText(t('ssh_minutes_label')), '30');
    expect(on.disabled).toBe(true); // not confirmed yet
    fireEvent.click(screen.getByLabelText(t('ssh_confirm_keys')));
    expect(on.disabled).toBe(false);
    await click(t('ssh_on'));
    expect(calls.find((c) => c.name === 'noxos.ssh.enable')?.payload).toEqual({
      minutes: 30,
      fingerprints: [FP],
      passphrase: PASS,
      pin: '4711',
    });
    const notice = screen.getAllByRole('status').find((n) => n.textContent?.includes('SSH ist an'));
    expect(notice).toBeTruthy();
  });

  it('switches off with one click and no PIN, and forgets the keys on request', async () => {
    const { calls } = await show({ 'noxos.ssh.status': ON });
    await click(t('ssh_off_now'));
    expect(calls.find((c) => c.name === 'noxos.ssh.disable')?.payload).toEqual({});
    expect(screen.getByText(t('ssh_off_state'))).toBeTruthy();
    await click(t('ssh_keys_forget'));
    expect(calls.filter((c) => c.name === 'noxos.ssh.disable').at(-1)?.payload).toEqual({ forget_keys: true });
  });

  it('says a PIN is needed when none is set, and offers no PIN field', async () => {
    await show({ 'security.pin.status': { configured: false } });
    expect(screen.getByText(t('ssh_needs_pin'))).toBeTruthy();
    expect(screen.queryByLabelText(t('lan_pair_pin'))).toBeNull();
  });

  it('without a passphrase, says it is set during setup and offers no keys or switch', async () => {
    await show({ 'noxos.ssh.status': status({ passphrase_set: false }) });
    expect(screen.getByText(t('ssh_passphrase_missing'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: t('ssh_keys_save') })).toBeNull();
    expect(screen.queryByRole('button', { name: t('ssh_passphrase_save') })).toBeNull();
  });

  it('during setup, sets the passphrase (at least 12 characters) with the PIN', async () => {
    const { calls } = await show({ 'noxos.ssh.status': status({ passphrase_set: false, setup_mode: true }) });
    const save = screen.getByRole('button', { name: t('ssh_passphrase_save') }) as HTMLButtonElement;
    type(screen.getByLabelText(t('lan_pair_pin')), '4711');
    type(screen.getByLabelText(t('ssh_passphrase_new')), 'too short');
    expect(save.disabled).toBe(true);
    type(screen.getByLabelText(t('ssh_passphrase_new')), PASS);
    expect(save.disabled).toBe(false);
    await click(t('ssh_passphrase_save'));
    expect(calls.find((c) => c.name === 'noxos.ssh.passphrase.set')?.payload).toEqual({ new: PASS, pin: '4711' });
    expect(screen.getByText(t('ssh_passphrase_saved'))).toBeTruthy();
    expect((screen.getByLabelText(t('ssh_passphrase_new')) as HTMLInputElement).value).toBe('');
  });

  it('changes the passphrase with the old one', async () => {
    const { calls } = await show();
    const pins = screen.getAllByLabelText(t('lan_pair_pin'));
    type(pins[pins.length - 1], '4711');
    type(screen.getByLabelText(t('ssh_passphrase_old')), PASS);
    type(screen.getByLabelText(t('ssh_passphrase_new')), 'a brand new passphrase');
    await click(t('ssh_passphrase_save'));
    expect(calls.find((c) => c.name === 'noxos.ssh.passphrase.change')?.payload).toEqual({
      old: PASS,
      new: 'a brand new passphrase',
      pin: '4711',
    });
  });

  it('says when the passphrase is locked and offers nothing to switch on', async () => {
    await show({
      'noxos.ssh.status': status({ keys: STORED, locked_until: '2026-10-22T13:00:00+00:00' }),
    });
    expect(screen.getByText((text) => text.startsWith('Zu viele falsche Wartungs-Passphrasen'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: t('ssh_on') })).toBeNull();
  });

  it('words a wrong passphrase and clears both secrets', async () => {
    await show({
      'noxos.ssh.status': status({ keys: STORED }),
      'noxos.ssh.enable': new IpcError('permission.denied', 'wrong', false, { reason: 'passphrase_wrong' }),
    });
    fireEvent.click(screen.getByLabelText(t('ssh_confirm_keys')));
    type(mainPin(), '4711');
    const secret = screen.getByLabelText(t('ssh_passphrase')) as HTMLInputElement;
    type(secret, 'not the passphrase');
    await click(t('ssh_on'));
    expect(screen.getByRole('alert').textContent).toBe(t('ssh_err_passphrase_wrong'));
    expect(secret.value).toBe('');
    expect(mainPin().value).toBe('');
  });

  it('words a refused key', async () => {
    await show({
      'noxos.ssh.keys.set': new IpcError('validation.failed', 'only ed25519', false, { reason: 'invalid_key', key: 'type' }),
    });
    type(screen.getByLabelText(t('ssh_keys_label')), 'ssh-rsa AAAA');
    type(mainPin(), '4711');
    type(screen.getByLabelText(t('ssh_passphrase')), PASS);
    await click(t('ssh_keys_save'));
    expect(screen.getByRole('alert').textContent).toBe(t('ssh_err_key'));
  });

  it('words a refusal to switch on with keys that are not the stored ones', async () => {
    await show({
      'noxos.ssh.status': status({ keys: STORED }),
      'noxos.ssh.enable': new IpcError('unavailable', 'not the ones confirmed', false, { reason: 'helper_refused' }),
    });
    fireEvent.click(screen.getByLabelText(t('ssh_confirm_keys')));
    type(mainPin(), '4711');
    type(screen.getByLabelText(t('ssh_passphrase')), PASS);
    await click(t('ssh_on'));
    expect(screen.getByRole('alert').textContent).toBe(t('ssh_err_helper'));
  });
});
