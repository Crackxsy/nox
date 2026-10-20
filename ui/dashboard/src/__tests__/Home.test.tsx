/**
 * Geräte › Zuhause: its designed states, and the two things it must never do.
 *
 * Never: show a toggle that looks as if it worked when Home Assistant refused it, and show a
 * device Nox is not allowed to touch. The first is why a failed switch gets its own line next to
 * itself rather than a banner at the top; the second is enforced in the core, and pinned here so a
 * future "let's just render everything the payload contains" cannot quietly undo it.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import { IpcError, type IpcClient } from '../ipc';
import {
  INITIAL_STATE,
  type DashboardState,
  applyHome,
  applyHomeStateChanged,
  parseHomeListing,
  reduceEvent,
} from '../model';
import { HomeRooms, useHome } from '../pages/Home';
import type { T } from '../i18n';

const t = translator('de');

const LISTING = {
  ok: true,
  connected: true,
  reason: '',
  areas: ['Wohnzimmer'],
  areas_available: true,
  entities: [
    {
      entity_id: 'light.wz_decke',
      name: 'Deckenlampe',
      area: 'Wohnzimmer',
      state: 'on',
      attributes: { brightness: 255 },
    },
    {
      entity_id: 'switch.kaffee',
      name: 'Kaffeemaschine',
      area: 'Wohnzimmer',
      state: 'off',
      attributes: {},
    },
  ],
};

const STATUS = {
  connected: true,
  reason: '',
  ha_version: '2026.9.0',
  host: '127.0.0.1',
  port: 8123,
  areas_available: true,
  token_present: true,
};

type Sent = { name: string; payload: unknown };

function fakeClient(
  overrides: Record<string, unknown> = {},
): { client: IpcClient; sent: Sent[] } {
  const sent: Sent[] = [];
  const client = {
    request: (name: string, payload: Record<string, unknown>) => {
      sent.push({ name, payload });
      if (name in overrides) return Promise.resolve(overrides[name]);
      if (name === 'home.status') return Promise.resolve(STATUS);
      if (name === 'home.list') return Promise.resolve(LISTING);
      return Promise.resolve({ ok: true, connected: true });
    },
  } as unknown as IpcClient;
  return { client, sent };
}

function refusing(error: unknown): IpcClient {
  return { request: () => Promise.reject(error) } as unknown as IpcClient;
}

async function renderPage(client: IpcClient | null, state: DashboardState = INITIAL_STATE) {
  let current = state;
  const onState = (updater: (s: DashboardState) => DashboardState) => {
    current = updater(current);
  };
  function Harness({ tr }: { tr: T }) {
    const home = useHome(tr, client, onState);
    return (
      <HomeRooms t={tr} lang="de" state={current} client={client} home={home} onOpenSettings={() => undefined} />
    );
  }
  await act(async () => {
    render(<Harness tr={t} />);
  });
  return () => current;
}

describe('Geräte › Zuhause', () => {
  it('explains a refused device list in German and keeps the core wording as a detail', async () => {
    const error = new IpcError('permission.denied', "role 'dashboard' may not call 'home.list'");
    await renderPage(refusing(error));
    await waitFor(() => expect(screen.getByText(t('home_refused'))).toBeDefined());
    expect(screen.getByText(t('home_refused_hint'))).toBeDefined();
    const detail = screen.getByText(/may not call 'home.list'/);
    expect(detail.className).toContain('detail-sub');
    // A designed state, not an error: nothing here is announced as an alert.
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says what to do when Home Assistant is simply not connected', async () => {
    const state: DashboardState = {
      ...INITIAL_STATE,
      home: applyHome(INITIAL_STATE.home, {
        connected: false,
        reason: 'no access token stored: nox/home/access_token',
        tokenPresent: false,
        host: '127.0.0.1',
        port: 8123,
      }),
    };
    await renderPage(null, state);
    expect(screen.getByText(t('home_disconnected'))).toBeDefined();
    expect(screen.getByText(t('home_no_token'))).toBeDefined();
    expect(screen.getByText(/nox\/home\/access_token/)).toBeDefined();
  });

  it('lists the devices of a room with their state', async () => {
    const state: DashboardState = {
      ...INITIAL_STATE,
      home: applyHome(INITIAL_STATE.home, parseHomeListing(LISTING)),
    };
    await renderPage(fakeClient().client, state);
    expect(screen.getByRole('heading', { level: 4, name: 'Wohnzimmer' })).toBeDefined();
    expect(screen.getByText('Deckenlampe')).toBeDefined();
    expect(screen.getByText(t('home_state_on'))).toBeDefined();
    expect(screen.getByText(t('home_state_off'))).toBeDefined();
    // The boundary, stated where a stranger reads it.
    expect(screen.getByText(t('home_locks_note'))).toBeDefined();
  });

  it('sends the right tool call for a light and for a socket', async () => {
    const state: DashboardState = {
      ...INITIAL_STATE,
      home: applyHome(INITIAL_STATE.home, parseHomeListing(LISTING)),
    };
    const { client, sent } = fakeClient();
    await renderPage(client, state);
    const lamp = screen.getByRole('switch', { name: 'Deckenlampe' });
    expect(lamp.getAttribute('aria-checked')).toBe('true');
    await act(async () => {
      await userEvent.click(lamp);
    });
    await act(async () => {
      await userEvent.click(screen.getByRole('switch', { name: 'Kaffeemaschine' }));
    });
    const calls = sent.filter((entry) => entry.name.startsWith('home.') && entry.name !== 'home.list' && entry.name !== 'home.status');
    expect(calls[0]).toEqual({
      name: 'home.light',
      payload: { entity_ids: ['light.wz_decke'], on: false },
    });
    expect(calls[1]).toEqual({
      name: 'home.switch',
      payload: { entity_ids: ['switch.kaffee'], on: true },
    });
  });

  it('shows a refused switch next to that switch, not as a page-wide banner', async () => {
    const state: DashboardState = {
      ...INITIAL_STATE,
      home: applyHome(INITIAL_STATE.home, parseHomeListing(LISTING)),
    };
    const { client } = fakeClient({
      'home.light': { ok: false, connected: true, reason: 'Home Assistant lehnt das ab' },
    });
    await renderPage(client, state);
    await act(async () => {
      await userEvent.click(screen.getByRole('switch', { name: 'Deckenlampe' }));
    });
    await waitFor(() =>
      expect(screen.getByText('Home Assistant lehnt das ab')).toBeDefined(),
    );
    // The failure sits in the lamp's own row, not in a banner; the other device is untouched.
    expect(screen.getByText('Home Assistant lehnt das ab').closest('li')!.textContent).toContain('Deckenlampe');
    expect(screen.getByRole('switch', { name: 'Kaffeemaschine' }).hasAttribute('disabled')).toBe(false);
  });
});

describe('the live state stream', () => {
  it('updates a listed entity and ignores one the listing does not contain', () => {
    const listed = applyHome(INITIAL_STATE.home, parseHomeListing(LISTING));
    const updated = applyHomeStateChanged(listed, {
      entity_id: 'light.wz_decke',
      state: 'off',
      attributes: {},
    });
    expect(updated.entities[0]?.state).toBe('off');

    const unknown = applyHomeStateChanged(listed, { entity_id: 'lock.haustuer', state: 'unlocked' });
    // Identity: an event for something the core never listed changes nothing at all.
    expect(unknown).toBe(listed);
  });

  it('is wired into the reducer under its event names', () => {
    const state: DashboardState = {
      ...INITIAL_STATE,
      home: applyHome(INITIAL_STATE.home, parseHomeListing(LISTING)),
    };
    const disconnected = reduceEvent(state, 'home.disconnected', { reason: 'nicht erreichbar' });
    expect(disconnected.home.connected).toBe(false);
    expect(disconnected.home.reason).toBe('nicht erreichbar');

    const connected = reduceEvent(disconnected, 'home.connected', { ha_version: '2026.9.0' });
    expect(connected.home.connected).toBe(true);
    expect(connected.home.haVersion).toBe('2026.9.0');
  });
});

describe('the parsers', () => {
  it('invents nothing for a payload the core did not send', () => {
    expect(parseHomeListing({})).toBeNull();
    expect(parseHomeListing(null)).toBeNull();
    const parsed = parseHomeListing({ entities: [{ name: 'no id' }, ...LISTING.entities] });
    expect(parsed?.entities?.length).toBe(2);
  });
});
