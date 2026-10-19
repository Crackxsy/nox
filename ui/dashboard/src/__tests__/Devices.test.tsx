/**
 * The "Devices" section: its states, pairing, the list, removing - on the PC and on a paired
 * device - and how it keeps itself current.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';
import { DevicesSection, DISARM_MS, PAIRED_BANNER_MS, type PairedNotice, REFRESH_MS } from '../pages/Devices';
import type { PlatformInput } from '../platform';

const t = translator('de');

const RUNNING = {
  enabled: true,
  running: true,
  error: '',
  warning: '',
  port: 47843,
  urls: ['https://192.168.1.20:47843/dashboard/'],
  fingerprint: 'AA:BB:CC:DD:EE:FF:00:11:22:33',
  ca_fingerprint: '11:22:33:44:55:66:77:88:99:00',
  ca_url: '/nox-ca.crt',
  pin_set: false,
};
const DEVICES = {
  enabled: true,
  devices: [
    { id: 'me', name: 'Tablet', paired_at: '2026-10-18T10:00:00+00:00', last_seen_at: null, revoked_at: null, current: true },
    { id: 'pc', name: 'Laptop', paired_at: '2026-10-18T09:00:00+00:00', last_seen_at: null, revoked_at: null, current: false },
    { id: 'old', name: 'Altes Handy', paired_at: '2026-10-17T09:00:00+00:00', last_seen_at: null, revoked_at: '2026-10-18T08:00:00+00:00', current: false },
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
  ca_fingerprint: RUNNING.ca_fingerprint,
};
const DESKTOP: PlatformInput = { userAgent: 'Mozilla/5.0 (Windows NT 10.0)', maxTouchPoints: 0, displayStandalone: false };
const IPHONE_SAFARI: PlatformInput = {
  userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)',
  maxTouchPoints: 5,
  displayStandalone: false,
};

type Answers = Record<string, unknown | ((payload: unknown) => Promise<unknown>)>;

function core(over: Answers = {}) {
  const calls: { name: string; payload: unknown }[] = [];
  const answers: Answers = {
    'lan.status': RUNNING,
    'lan.devices.list': DEVICES,
    'lan.pair.start': PAIRING,
    'lan.unpair': { ok: true },
    ...over,
  };
  const client = {
    request: vi.fn((name: string, payload: unknown) => {
      calls.push({ name, payload });
      const answer = answers[name];
      return typeof answer === 'function' ? answer(payload) : Promise.resolve(answer);
    }),
  } as unknown as IpcClient;
  return { client, calls };
}

interface ShowOptions {
  device?: boolean;
  answers?: Answers;
  platform?: PlatformInput;
}

async function show({ device = false, answers = {}, platform = DESKTOP }: ShowOptions = {}) {
  const { client, calls } = core(answers);
  const signedOut = vi.fn();
  const openSettings = vi.fn();
  const props = {
    t,
    lang: 'de' as const,
    client,
    device,
    revision: 0,
    justPaired: null as PairedNotice | null,
    onOpenSettings: openSettings,
    onSignedOut: signedOut,
    platform,
  };
  let view: ReturnType<typeof render> | undefined;
  await act(async () => {
    view = render(<DevicesSection {...props} />);
  });
  const rerender = async (over: Partial<typeof props>) => {
    await act(async () => {
      view!.rerender(<DevicesSection {...props} {...over} />);
    });
  };
  return { calls, signedOut, openSettings, rerender, client };
}

const pairingStarts = (calls: { name: string; payload: unknown }[]) => calls.filter((c) => c.name === 'lan.pair.start');

afterEach(() => {
  vi.useRealTimers();
});

describe('the states before there is anything to pair', () => {
  it('says it is loading while Nox has not answered', async () => {
    await show({ answers: { 'lan.status': () => new Promise(() => undefined) } });
    expect(screen.getByRole('status').textContent).toBe('Lädt …');
  });

  it('says network access is off and leads the PC to the one setting', async () => {
    const { openSettings } = await show({ answers: { 'lan.status': { ...RUNNING, enabled: false, running: false } } });
    expect(screen.getByRole('heading', { name: t('lan_off_title') })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: t('lan_off_settings') }));
    expect(openSettings).toHaveBeenCalledOnce();
  });

  it('offers a paired device no settings link it could not use', async () => {
    await show({ device: true, answers: { 'lan.status': { ...RUNNING, enabled: false, running: false } } });
    expect(screen.queryByRole('button', { name: t('lan_off_settings') })).toBeNull();
  });

  it('treats only an unknown request as "off"', async () => {
    await show({ answers: { 'lan.status': () => Promise.reject(new IpcError('not_found', "unknown request 'lan.status'")) } });
    expect(screen.getByRole('heading', { name: t('lan_off_title') })).toBeTruthy();
  });

  it('says it could not read an answer instead of showing an empty list', async () => {
    await show({ answers: { 'lan.devices.list': { devices: 'nope' } } });
    expect(screen.getByText(t('lan_unreadable'))).toBeTruthy();
    expect(screen.queryByText(t('lan_devices_empty'))).toBeNull();
    expect(screen.queryByRole('heading', { name: t('lan_off_title') })).toBeNull();
  });

  it('says a real failure in words, never the core\'s English, and can try again', async () => {
    let fail = true;
    const { calls } = await show({
      answers: {
        'lan.status': () =>
          fail
            ? Promise.reject(new IpcError('unavailable', 'network access is starting', true, { reason: 'starting' }))
            : Promise.resolve(RUNNING),
      },
    });
    expect(screen.getByRole('heading', { name: t('lan_load_failed') })).toBeTruthy();
    expect(screen.getByText(t('reason_starting'))).toBeTruthy();
    expect(document.body.textContent).not.toContain('network access is starting');
    fail = false;
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_retry') }));
    });
    expect(screen.getByRole('heading', { name: t('lan_pair_title') })).toBeTruthy();
    expect(calls.filter((c) => c.name === 'lan.status')).toHaveLength(2);
  });

  it('keeps the core\'s own reason out of the lede when the listener failed', async () => {
    await show({ answers: { 'lan.status': { ...RUNNING, running: false, error: 'certificate: /var/lib/nox/tls/server.key unreadable' } } });
    const tile = screen.getByRole('heading', { name: t('lan_pair_title') }).closest('section')!;
    expect(within(tile).getByText(t('lan_failed'))).toBeTruthy();
    const details = within(tile).getByText(t('lan_technical')).closest('details')!;
    expect(details.textContent).toContain('server.key');
    expect(screen.getByRole('button', { name: t('lan_pair_button') })).toHaveProperty('disabled', true);
  });
});

describe('pairing a new device', () => {
  it('shows the QR code, the link, the code spelled out and the server fingerprint', async () => {
    const { calls } = await show();
    fireEvent.change(screen.getByLabelText(t('lan_pair_name')), { target: { value: 'Küche' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    expect(pairingStarts(calls)[0]?.payload).toEqual({ name: 'Küche' });
    expect(screen.getByRole('img', { name: t('lan_pair_qr_alt') }).getAttribute('src')).toBe(PAIRING.qr);
    expect(screen.getByRole('link', { name: PAIRING.url }).getAttribute('href')).toBe(PAIRING.url);
    expect(screen.getByText('ABCD EFGH').getAttribute('aria-hidden')).toBe('true');
    expect(screen.getByText('Code: A B C D E F G H')).toBeTruthy();
    expect(screen.getByText('AA:BB:CC:DD:EE:FF:00:11')).toBeTruthy();
    expect(screen.getByText(t('lan_fingerprint_hint'))).toBeTruthy();
    // The one live line says the code is ready.
    const tile = screen.getByRole('heading', { name: t('lan_pair_title') }).closest('section')!;
    expect(within(tile).getByRole('status').textContent).toContain('Code erzeugt.');
  });

  it('can throw a code away', async () => {
    await show();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    fireEvent.click(screen.getByRole('button', { name: t('lan_pair_discard') }));
    expect(screen.queryByRole('img', { name: t('lan_pair_qr_alt') })).toBeNull();
    expect(screen.getByRole('button', { name: t('lan_pair_button') })).toBeTruthy();
  });

  it('says so when copying the link did not work', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new Error('denied')) },
    });
    await show();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_copy') }));
    });
    expect(screen.getByText(t('lan_pair_copy_failed'))).toBeTruthy();
  });

  it('asks the PC for the PIN when one is set, and sends it', async () => {
    const { calls } = await show({ answers: { 'lan.status': { ...RUNNING, pin_set: true } } });
    fireEvent.change(screen.getByLabelText(t('lan_pair_pin')), { target: { value: '4711' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    expect(pairingStarts(calls)[0]?.payload).toEqual({ name: '', pin: '4711' });
  });

  it('on the PC without a PIN set, asks for none', async () => {
    await show();
    expect(screen.queryByLabelText(t('lan_pair_pin'))).toBeNull();
  });

  it('shows the PIN field when a refusal says a PIN was set meanwhile', async () => {
    let pinSet = false;
    await show({
      answers: {
        'lan.status': () => Promise.resolve({ ...RUNNING, pin_set: pinSet }),
        'lan.pair.start': () => {
          pinSet = true;
          return Promise.reject(new IpcError('permission.denied', 'lan.pair requires the security PIN'));
        },
      },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    expect(screen.getByRole('alert').textContent).toBe(t('pin_required'));
    expect(screen.getByLabelText(t('lan_pair_pin'))).toBeTruthy();
  });

  it('asks a paired device for the PIN and sends it', async () => {
    const { calls } = await show({ device: true, answers: { 'lan.status': { ...RUNNING, pin_set: true } } });
    fireEvent.change(screen.getByLabelText(t('lan_pair_pin')), { target: { value: '4711' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    expect(pairingStarts(calls)[0]?.payload).toEqual({ name: '', pin: '4711' });
  });

  it('shows a paired device no form that cannot work when no PIN is set', async () => {
    await show({ device: true });
    expect(screen.getByText(t('lan_pair_only_at_computer'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: t('lan_pair_button') })).toBeNull();
  });

  it('says a refusal in words', async () => {
    await show({
      device: true,
      answers: {
        'lan.status': { ...RUNNING, pin_set: true },
        'lan.pair.start': () =>
          Promise.reject(new IpcError('permission.denied', 'lan.pair denied: wrong pin', false, { reason: 'pin_wrong' })),
      },
    });
    fireEvent.change(screen.getByLabelText(t('lan_pair_pin')), { target: { value: '0000' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    expect(screen.getByRole('alert').textContent).toBe(t('reason_pin_wrong'));
  });

  it('closes the code, clears the name and says who arrived - for a few seconds', async () => {
    const { rerender } = await show();
    fireEvent.change(screen.getByLabelText(t('lan_pair_name')), { target: { value: 'Küche' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_pair_button') }));
    });
    vi.useFakeTimers();
    await rerender({ revision: 1, justPaired: { name: 'Küche', seq: 1 } });
    expect(screen.getByText('Küche ist gekoppelt.')).toBeTruthy();
    expect(screen.queryByRole('img', { name: t('lan_pair_qr_alt') })).toBeNull();
    expect((screen.getByLabelText(t('lan_pair_name')) as HTMLInputElement).value).toBe('');
    act(() => {
      vi.advanceTimersByTime(PAIRED_BANNER_MS + 10);
    });
    expect(screen.queryByText('Küche ist gekoppelt.')).toBeNull();
  });
});

describe('the list', () => {
  it('marks this device and says "entfernt" for a removed one', async () => {
    await show({ device: true });
    const list = await screen.findByRole('list', { name: t('lan_devices_title') });
    const [mine, , old] = within(list).getAllByRole('listitem');
    expect(mine!.textContent).toContain(t('lan_this_device'));
    expect(old!.textContent).toContain('entfernt');
    expect(old!.textContent).not.toContain('widerrufen');
  });

  it('reloads when the page becomes visible again and every 30 seconds while it is', async () => {
    const { calls } = await show();
    const lists = () => calls.filter((c) => c.name === 'lan.devices.list').length;
    expect(lists()).toBe(1);
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(lists()).toBe(2);
    vi.useFakeTimers();
    // A fresh interval runs under the fake clock after the next visibility change.
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(lists()).toBe(3);
    await act(async () => {
      vi.advanceTimersByTime(REFRESH_MS);
    });
    expect(lists()).toBe(4);
  });
});

describe('removing a device', () => {
  it('needs a second press on a button that stays put, then removes it', async () => {
    const { calls } = await show();
    const row = within(await screen.findByRole('list', { name: t('lan_devices_title') }))
      .getAllByRole('listitem')
      .find((li) => li.textContent?.includes('Laptop'))!;
    const button = within(row).getByRole('button', { name: t('lan_remove') });
    fireEvent.click(button);
    expect(calls.some((c) => c.name === 'lan.unpair')).toBe(false);
    // The same element, now named for what the second press does.
    expect(within(row).getByRole('button', { name: t('lan_remove_armed') })).toBe(button);
    expect(within(row).getByRole('status').textContent).toBe(
      'Zum Bestätigen noch einmal auf „Wirklich entfernen“. Danach muss Laptop neu gekoppelt werden.',
    );
    await act(async () => {
      fireEvent.click(button);
    });
    expect(calls.find((c) => c.name === 'lan.unpair')?.payload).toEqual({ device_id: 'pc' });
  });

  it('disarms by itself after a while', async () => {
    await show();
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole('button', { name: t('lan_remove') }));
    expect(screen.getByRole('button', { name: t('lan_remove_armed') })).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(DISARM_MS + 10);
    });
    expect(screen.queryByRole('button', { name: t('lan_remove_armed') })).toBeNull();
  });

  it('lets a paired device sign itself out, and nothing else when no PIN is set', async () => {
    const { calls, signedOut } = await show({ device: true });
    await waitFor(() =>
      expect(within(screen.getByRole('list', { name: t('lan_devices_title') })).getAllByRole('listitem')).toHaveLength(3),
    );
    expect(screen.queryByRole('button', { name: t('lan_remove') })).toBeNull();
    const signOut = screen.getByRole('button', { name: t('lan_sign_out') });
    fireEvent.click(signOut);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('lan_sign_out_armed') }));
    });
    expect(calls.find((c) => c.name === 'lan.unpair')?.payload).toEqual({ device_id: 'me' });
    expect(signedOut).toHaveBeenCalledOnce();
  });

  it('lets a paired device remove another one with the PIN', async () => {
    const { calls } = await show({ device: true, answers: { 'lan.status': { ...RUNNING, pin_set: true } } });
    const remove = screen.getByRole('button', { name: t('lan_remove') });
    fireEvent.click(remove);
    await act(async () => {
      fireEvent.click(remove);
    });
    expect(calls.some((c) => c.name === 'lan.unpair')).toBe(false);
    expect(screen.getByRole('alert').textContent).toBe(t('reason_pin_required'));
    fireEvent.change(screen.getByLabelText('Sicherheits-PIN zum Entfernen von Laptop'), { target: { value: '4711' } });
    await act(async () => {
      fireEvent.click(remove);
    });
    expect(calls.find((c) => c.name === 'lan.unpair')?.payload).toEqual({ device_id: 'pc', pin: '4711' });
  });
});

describe('the certificate on a paired device', () => {
  it('offers the download and the authority\'s own fingerprint to check it', async () => {
    await show({ device: true });
    expect(screen.getByRole('link', { name: t('lan_ca_download') }).getAttribute('href')).toBe('/nox-ca.crt');
    expect(screen.getByText(t('lan_ca_fingerprint'))).toBeTruthy();
    expect(screen.getByText('11:22:33:44:55:66:77:88')).toBeTruthy();
    expect(screen.queryByText(t('lan_ca_ios_order'))).toBeNull();
  });

  it('tells Safari on iOS the order that works: certificate, home screen, pair in the app', async () => {
    await show({ device: true, platform: IPHONE_SAFARI });
    expect(screen.getByText(t('lan_ca_ios_order'))).toBeTruthy();
    expect(screen.getByText(t('lan_ca_ios_step_pair'))).toBeTruthy();
  });

  it('does not tell the installed app to install itself', async () => {
    await show({ device: true, platform: { ...IPHONE_SAFARI, standalone: true } });
    expect(screen.queryByText(t('lan_ca_ios_order'))).toBeNull();
  });
});
