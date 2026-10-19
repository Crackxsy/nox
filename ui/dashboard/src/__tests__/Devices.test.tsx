/**
 * The "Devices" section: off, pairing, the list, removing - on the PC and on a paired device.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import type { IpcClient } from '../ipc';
import { DevicesSection } from '../pages/Devices';

const t = translator('de');

const RUNNING = {
  enabled: true,
  running: true,
  error: '',
  urls: ['https://192.168.1.20:47843/dashboard/'],
  fingerprint: 'AA:BB:CC:DD:EE:FF:00:11:22:33',
};
const DEVICES = {
  enabled: true,
  devices: [
    { id: 'me', name: 'Tablet', paired_at: '2026-10-18T10:00:00+00:00', last_seen_at: null, revoked_at: null, current: true },
    { id: 'pc', name: 'Laptop', paired_at: '2026-10-18T09:00:00+00:00', last_seen_at: null, revoked_at: null, current: false },
  ],
};
const PAIRING = {
  code: 'ABCDEFGH',
  expires_at: new Date(Date.now() + 300_000).toISOString(),
  ttl_s: 300,
  url: 'https://192.168.1.20:47843/dashboard/#pair=ABCDEFGH',
  urls: ['https://192.168.1.20:47843/dashboard/#pair=ABCDEFGH'],
  qr: 'data:image/svg+xml;charset=utf-8,%3Csvg%3E',
  fingerprint: RUNNING.fingerprint,
};

function core(status: unknown = RUNNING) {
  const calls: { name: string; payload: unknown }[] = [];
  const answers: Record<string, unknown> = {
    'lan.status': status,
    'lan.devices.list': DEVICES,
    'lan.pair.start': PAIRING,
    'lan.unpair': { ok: true },
  };
  const client = {
    request: vi.fn((name: string, payload: unknown) => {
      calls.push({ name, payload });
      return Promise.resolve(answers[name]);
    }),
  } as unknown as IpcClient;
  return { client, calls };
}

async function show(device: boolean, status: unknown = RUNNING) {
  const { client, calls } = core(status);
  const signedOut = vi.fn();
  const openSettings = vi.fn();
  await act(async () => {
    render(
      <DevicesSection
        t={t}
        lang="de"
        client={client}
        device={device}
        revision={0}
        justPaired=""
        onOpenSettings={openSettings}
        onSignedOut={signedOut}
      />,
    );
  });
  return { calls, signedOut, openSettings };
}

describe('when network access is off', () => {
  it('says so and leads the PC to the one setting', async () => {
    const { openSettings } = await show(false, { ...RUNNING, enabled: false, running: false });
    expect(screen.getByRole('heading', { name: t('lan_off_title') }).textContent).toBe(t('lan_off_title'));
    fireEvent.click(screen.getByRole('button', { name: t('lan_off_settings') }));
    expect(openSettings).toHaveBeenCalledOnce();
  });

  it('offers a paired device no settings link it could not use', async () => {
    await show(true, { ...RUNNING, enabled: false, running: false });
    expect(screen.queryByRole('button', { name: t('lan_off_settings') })).toBeNull();
  });
});

describe('pairing a new device', () => {
  it('shows the QR code, the link, the grouped code and the fingerprint', async () => {
    const { calls } = await show(false);
    fireEvent.change(screen.getByLabelText(t('lan_pair_name')), { target: { value: 'Küche' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    expect(calls.find((c) => c.name === 'lan.pair.start')?.payload).toEqual({ name: 'Küche' });
    const qr = screen.getByRole('img', { name: t('lan_pair_qr_alt') });
    expect(qr.getAttribute('src')).toBe(PAIRING.qr);
    expect(screen.getByRole('link', { name: PAIRING.url }).getAttribute('href')).toBe(PAIRING.url);
    expect(screen.getByText('ABCD EFGH').textContent).toBe('ABCD EFGH');
    expect(screen.getByText('AA:BB:CC:DD:EE:FF:00:11').textContent).toBe('AA:BB:CC:DD:EE:FF:00:11');
  });

  it('asks a paired device for the PIN and sends it', async () => {
    const { calls } = await show(true);
    fireEvent.change(screen.getByLabelText(t('lan_pair_pin')), { target: { value: '4711' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    expect(calls.find((c) => c.name === 'lan.pair.start')?.payload).toEqual({ name: '', pin: '4711' });
  });
});

describe('removing a device', () => {
  it('needs a second press, then removes it', async () => {
    const { calls } = await show(false);
    const remove = await screen.findByRole('button', { name: t('lan_remove') });
    fireEvent.click(remove);
    expect(calls.some((c) => c.name === 'lan.unpair')).toBe(false);
    expect(screen.getByText(/Noch einmal tippen, um Laptop zu entfernen/).textContent).toContain('Laptop');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_remove') }));
    });
    expect(calls.find((c) => c.name === 'lan.unpair')?.payload).toEqual({ device_id: 'pc' });
  });

  it('lets a paired device sign only itself out', async () => {
    const { calls, signedOut } = await show(true);
    await waitFor(() =>
      expect(within(screen.getByRole('list', { name: t('lan_devices_title') })).getAllByRole('listitem')).toHaveLength(2),
    );
    expect(screen.queryByRole('button', { name: t('lan_remove') })).toBeNull();
    const signOut = screen.getByRole('button', { name: t('lan_sign_out') });
    fireEvent.click(signOut);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_sign_out') }));
    });
    expect(calls.find((c) => c.name === 'lan.unpair')?.payload).toEqual({ device_id: 'me' });
    expect(signedOut).toHaveBeenCalledOnce();
  });

  it('marks this device in the list', async () => {
    await show(true);
    const list = await screen.findByRole('list', { name: t('lan_devices_title') });
    const mine = within(list).getAllByRole('listitem')[0]!;
    expect(mine.textContent).toContain(t('lan_this_device'));
  });
});

describe('on a paired device', () => {
  it('offers the certificate for a warning-free start', async () => {
    await show(true);
    const download = screen.getByRole('link', { name: t('lan_ca_download') });
    expect(download.getAttribute('href')).toBe('/nox-ca.crt');
  });
});
