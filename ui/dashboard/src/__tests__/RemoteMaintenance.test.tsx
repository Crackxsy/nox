/**
 * Einstellungen › System › Fernwartung (SSH): shown only on Nox-OS, on with PIN + key + time,
 * a visible notice while on, off with one click, refusals in words - against a scripted core.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { RemoteMaintenance, parseSshStatus } from '../areas/settings/RemoteMaintenance';
import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';

const t = translator('de');
const KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f admin@example.invalid';

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
  active_keys: ['SHA256:fictitiousfingerprint'],
});

type Answers = Record<string, unknown>;

function core(over: Answers = {}) {
  const calls: { name: string; payload: Record<string, unknown> }[] = [];
  const answers: Answers = {
    'noxos.ssh.status': status(),
    'security.pin.status': { configured: true },
    'noxos.ssh.enable': ON,
    'noxos.ssh.disable': status(),
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

  it('switches on with key, time and PIN, then says it is on and until when', async () => {
    const { calls } = await show();
    expect(screen.getByText(t('ssh_off_state'))).toBeTruthy();
    const button = screen.getByRole('button', { name: t('ssh_on') });
    expect((button as HTMLButtonElement).disabled).toBe(true); // no key, no PIN yet
    fireEvent.change(screen.getByLabelText(t('ssh_keys_label')), { target: { value: KEY } });
    fireEvent.change(screen.getByLabelText(t('ssh_minutes_label')), { target: { value: '30' } });
    fireEvent.change(screen.getByLabelText(t('lan_pair_pin')), { target: { value: '4711' } });
    await act(async () => {
      fireEvent.click(button);
    });
    const enable = calls.find((c) => c.name === 'noxos.ssh.enable');
    expect(enable?.payload).toEqual({ minutes: 30, pin: '4711', keys: KEY });
    const notice = screen.getByRole('status');
    expect(notice.textContent).toContain('SSH ist an');
    expect(screen.getByText('SHA256:fictitiousfingerprint')).toBeTruthy();
  });

  it('switches off with one click and no PIN', async () => {
    const { calls } = await show({ 'noxos.ssh.status': ON });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('ssh_off_now') }));
    });
    expect(calls.find((c) => c.name === 'noxos.ssh.disable')?.payload).toEqual({});
    expect(screen.getByText(t('ssh_off_state'))).toBeTruthy();
  });

  it('says a PIN is needed when none is set, and offers no PIN field', async () => {
    await show({ 'security.pin.status': { configured: false } });
    expect(screen.getByText(t('ssh_needs_pin'))).toBeTruthy();
    expect(screen.queryByLabelText(t('lan_pair_pin'))).toBeNull();
  });

  it('words a refused key and clears the PIN', async () => {
    await show({
      'noxos.ssh.enable': new IpcError('validation.failed', 'only ed25519', false, { reason: 'invalid_key', key: 'type' }),
    });
    fireEvent.change(screen.getByLabelText(t('ssh_keys_label')), { target: { value: 'ssh-rsa AAAA' } });
    const pin = screen.getByLabelText(t('lan_pair_pin')) as HTMLInputElement;
    fireEvent.change(pin, { target: { value: '4711' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('ssh_on') }));
    });
    expect(screen.getByRole('alert').textContent).toBe(t('ssh_err_key'));
    expect(pin.value).toBe('');
  });

  it('uses the saved keys without pasting them again', async () => {
    const { calls } = await show({
      'noxos.ssh.status': status({ keys: [{ type: 'ssh-ed25519', fingerprint: 'SHA256:saved', comment: 'laptop' }] }),
    });
    expect(screen.getByText('SHA256:saved')).toBeTruthy();
    fireEvent.change(screen.getByLabelText(t('lan_pair_pin')), { target: { value: '4711' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('ssh_on') }));
    });
    expect(calls.find((c) => c.name === 'noxos.ssh.enable')?.payload).toEqual({ minutes: 60, pin: '4711' });
  });
});
