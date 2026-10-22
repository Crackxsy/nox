/**
 * Einstellungen › System › Fernwartung (SSH): shown only on Nox-OS; keys first (PIN), shown by
 * their fingerprints; on only after the owner confirmed exactly those (PIN, time); a visible
 * notice while on; off and forgetting with one click; refusals in words - against a scripted core.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { RemoteMaintenance, parseSshStatus } from '../areas/settings/RemoteMaintenance';
import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';

const t = translator('de');
const KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f admin@example.invalid';
const FP = 'SHA256:fictitiousFingerprintOfAKeyNobodyOwns000000';
const STORED = [{ type: 'ssh-ed25519', fingerprint: FP, comment: 'laptop' }];

function status(over: Record<string, unknown> = {}) {
  return {
    available: true,
    reason: '',
    on: false,
    until: null,
    seconds_left: null,
    active_keys: [],
    keys: [],
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

  it('stores keys first and shows their fingerprints, switching nothing on', async () => {
    const { calls } = await show();
    const save = screen.getByRole('button', { name: t('ssh_keys_save') }) as HTMLButtonElement;
    expect(save.disabled).toBe(true); // no key, no PIN yet
    fireEvent.change(screen.getByLabelText(t('ssh_keys_label')), { target: { value: KEY } });
    fireEvent.change(screen.getByLabelText(t('lan_pair_pin')), { target: { value: '4711' } });
    await click(t('ssh_keys_save'));
    expect(calls.find((c) => c.name === 'noxos.ssh.keys.set')?.payload).toEqual({ keys: KEY, pin: '4711' });
    expect(calls.some((c) => c.name === 'noxos.ssh.enable')).toBe(false);
    expect(screen.getByText(FP)).toBeTruthy();
    expect(screen.getByText(t('ssh_off_state'))).toBeTruthy();
  });

  it('switches on only after the fingerprints are confirmed, naming exactly those', async () => {
    const { calls } = await show({ 'noxos.ssh.status': status({ keys: STORED }) });
    const on = screen.getByRole('button', { name: t('ssh_on') }) as HTMLButtonElement;
    fireEvent.change(screen.getByLabelText(t('lan_pair_pin')), { target: { value: '4711' } });
    fireEvent.change(screen.getByLabelText(t('ssh_minutes_label')), { target: { value: '30' } });
    expect(on.disabled).toBe(true); // not confirmed yet
    fireEvent.click(screen.getByLabelText(t('ssh_confirm_keys')));
    expect(on.disabled).toBe(false);
    await click(t('ssh_on'));
    expect(calls.find((c) => c.name === 'noxos.ssh.enable')?.payload).toEqual({
      minutes: 30,
      fingerprints: [FP],
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

  it('words a refused key and clears the PIN', async () => {
    await show({
      'noxos.ssh.keys.set': new IpcError('validation.failed', 'only ed25519', false, { reason: 'invalid_key', key: 'type' }),
    });
    fireEvent.change(screen.getByLabelText(t('ssh_keys_label')), { target: { value: 'ssh-rsa AAAA' } });
    const pin = screen.getByLabelText(t('lan_pair_pin')) as HTMLInputElement;
    fireEvent.change(pin, { target: { value: '4711' } });
    await click(t('ssh_keys_save'));
    expect(screen.getByRole('alert').textContent).toBe(t('ssh_err_key'));
    expect(pin.value).toBe('');
  });

  it('words a refusal to switch on with keys that are not the stored ones', async () => {
    await show({
      'noxos.ssh.status': status({ keys: STORED }),
      'noxos.ssh.enable': new IpcError('unavailable', 'not the ones confirmed', false, { reason: 'helper_refused' }),
    });
    fireEvent.click(screen.getByLabelText(t('ssh_confirm_keys')));
    fireEvent.change(screen.getByLabelText(t('lan_pair_pin')), { target: { value: '4711' } });
    await click(t('ssh_on'));
    expect(screen.getByRole('alert').textContent).toBe(t('ssh_err_helper'));
  });
});
