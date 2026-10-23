/**
 * axe-core on the shell and every area (Spec UX-Neubau §10: WCAG 2.2 AA, zero findings).
 *
 * jsdom has no layout, so the two rules that need one - colour contrast and target size - are
 * measured elsewhere: contrast by `tokens.test.ts` on the palette itself, and both by the headless
 * click tests (`tests/e2e/test_ux_redesign_stubbed.py`), which run axe in a real browser at three
 * sizes in light and dark.
 */

import { act, cleanup, render, screen, within } from '@testing-library/react';
import axe from 'axe-core';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Auth } from '../auth';
import type { DashboardIpcHandlers } from '../ipc';

const harness = vi.hoisted(() => ({
  handlers: null as DashboardIpcHandlers | null,
  answers: {} as Record<string, unknown>,
}));

vi.mock('../ipc', async (importOriginal) => {
  const real = await importOriginal<typeof import('../ipc')>();
  return {
    ...real,
    createDashboardClient: vi.fn(async (_auth: Auth, handlers: DashboardIpcHandlers) => {
      harness.handlers = handlers;
      return { request: (name: string) => Promise.resolve(harness.answers[name] ?? {}), close: () => undefined };
    }),
  };
});

const { App } = await import('../App');
const { translator } = await import('../i18n');
const t = translator('de');

const NO_LAYOUT = { 'color-contrast': { enabled: false }, 'target-size': { enabled: false } };

async function violations(): Promise<string[]> {
  const result = await axe.run(document.body, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] },
    rules: NO_LAYOUT,
  });
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(' | ')}`);
}

async function start(hash: string, auth: Auth = { kind: 'local', token: 'tok' }, lang: 'de' | 'en' = 'de') {
  window.history.replaceState(null, '', hash);
  await act(async () => {
    render(<App auth={auth} lang={lang} theme="system" />);
  });
  await act(async () => {
    harness.handlers!.onStatus('online');
  });
  // Lazy areas and their copy.
  await act(async () => new Promise((r) => setTimeout(r, 30)));
}

async function emit(name: string, payload: Record<string, unknown>) {
  await act(async () => {
    harness.handlers!.onEvent({
      v: 1,
      id: `e-${Math.random()}`,
      ts: new Date().toISOString(),
      kind: 'event',
      name,
      corr: null,
      src: { role: 'core', id: 'core' },
      payload,
    });
  });
}

const prompt = {
  request_id: 'p1',
  agent: 'companion',
  tool: 'desktop',
  action: 'process_start',
  mode: 'companion',
  risk: 'medium',
  target: 'OBS Studio',
  expires_at: new Date(Date.now() + 60_000).toISOString(),
  remember_ttl_s: 3600,
};

beforeEach(() => {
  harness.answers = {
    'state.get': {
      assistant: { mode: 'companion', muted: false },
      privacy: { mode: 'balanced', microphone: true },
      system: { level: 'running' },
    },
    'memory.profile.list': { items: [{ id: 1, text: 'Mag Tee.', created_at: new Date().toISOString(), learned: true, unconfirmed: true }] },
    'home.status': { connected: true, host: 'ha', port: 8123, token_present: true },
    'home.list': {
      ok: true,
      connected: true,
      areas: ['Wohnzimmer'],
      areas_available: true,
      entities: [{ entity_id: 'light.a', name: 'Deckenlampe', area: 'Wohnzimmer', state: 'on', attributes: {} }],
    },
    'config.get': {
      values: { 'identity.user_display_name': 'Mila' },
      schema: [
        { path: 'identity.user_display_name', type: 'string', group: 'identity' },
        { path: 'desktop.computer_use.enabled', type: 'bool', group: 'privacy' },
        { path: 'voice.stt.listening_mode', type: 'enum', options: ['continuous', 'ptt_only'], group: 'voice' },
      ],
    },
  };
});

describe('axe-core: zero findings', () => {
  it('would see a finding (the check itself works in this environment)', async () => {
    render(<button type="button" />);
    expect((await violations()).some((v) => v.startsWith('button-name'))).toBe(true);
  });

  it('on Heute, with a question inline and the toggles of Zuhause', async () => {
    await start('#/today');
    await emit('security.permission_requested', prompt);
    await screen.findByRole('region', { name: t('ux_perm_q_process_start') });
    expect(await violations()).toEqual([]);
  });

  it('in the command palette, the Modus menu and the privacy menu', async () => {
    await start('#/today');
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }));
    });
    const palette = await screen.findByRole('dialog', { name: t('ux_palette') });
    await act(async () => {
      within(palette).getByRole('combobox').focus();
    });
    expect(await violations()).toEqual([]);
    await act(async () => within(palette).getByRole('button', { name: t('ux_close') }).click());

    await act(async () => screen.getByRole('button', { name: new RegExp(t('ux_mode_open')) }).click());
    await screen.findAllByRole('radio');
    expect(await violations()).toEqual([]);
  });

  it.each([
    '#/nox/talk',
    '#/nox/views',
    '#/nox/clips',
    '#/nox/history',
    '#/devices/devices',
    '#/devices/skills',
    '#/settings',
    '#/settings/privacy',
    '#/settings/voice',
    '#/settings/stream',
    '#/settings/you',
    '#/settings/nox',
    '#/settings/home',
    '#/settings/ai',
    '#/settings/system',
  ])('on %s', async (hash) => {
    await start(hash);
    expect(await violations()).toEqual([]);
  });

  it.each([false, true])('on System on Nox-OS, remote maintenance on: %s', async (on) => {
    harness.answers['security.pin.status'] = { configured: true };
    harness.answers['noxos.ssh.status'] = {
      available: true,
      on,
      until: on ? new Date(Date.now() + 3_600_000).toISOString() : null,
      active_keys: on ? ['SHA256:fictitious'] : [],
      keys: [{ type: 'ssh-ed25519', fingerprint: 'SHA256:fictitious', comment: 'laptop' }],
      passphrase_set: true,
      default_minutes: 60,
      min_minutes: 5,
      max_minutes: 240,
    };
    await start('#/settings/system');
    expect(screen.getByText(t('ssh_title'))).toBeTruthy();
    expect(await violations()).toEqual([]);
  });

  it('while stopped, and with a question as a sheet', async () => {
    await start('#/settings');
    await emit('security.kill_switch', { origin: 'ui' });
    expect(await violations()).toEqual([]);
    await emit('security.permission_requested', { ...prompt, request_id: 'p2' });
    await screen.findByRole('alertdialog');
    expect(await violations()).toEqual([]);
  });

  it('on Geräte with computers, in the sheet of one and in the add sheet', async () => {
    const pc = 'a'.repeat(32);
    harness.answers['node.list'] = {
      enabled: true,
      nodes: [
        {
          id: pc,
          name: 'Arbeits-PC',
          label: 'Arbeits-PC (aaaa)',
          online: true,
          compatible: true,
          os: 'windows',
          arch: 'AMD64',
          version: '0.7.0',
          paired_at: '2026-10-20T09:00:00+00:00',
          last_seen_at: null,
          revoked: false,
          tools: ['system.info', 'notify.show'],
          groups: [
            { group: 'system', enabled_on_device: true, narrowed: false, active: true },
            { group: 'notify', enabled_on_device: true, narrowed: true, active: false },
            { group: 'shell', enabled_on_device: false, narrowed: false, active: false },
          ],
        },
      ],
      defaults: [{ group: 'notify', tools: ['notify.show'], device: pc, raw: pc }],
    };
    harness.answers['node.pair.start'] = {
      code: 'K7F29QAB',
      expires_at: new Date(Date.now() + 300_000).toISOString(),
      link: 'nox-node://pair?h=192.168.1.20&p=47843&c=K7F29QAB&fp=' + 'ab'.repeat(32),
      qr: 'data:image/svg+xml;charset=utf-8,%3Csvg%3E',
      ca_fingerprint: '11:22:33:44:55:66:77:88',
    };
    await start('#/devices/devices');
    const open = await screen.findByRole('button', { name: 'Arbeits-PC öffnen' });
    expect(await violations()).toEqual([]);
    await act(async () => open.click());
    const sheet = await screen.findByRole('dialog', { name: 'Arbeits-PC' });
    await act(async () => within(sheet).getByRole('switch', { name: 'Befehle ausführen' }).click());
    expect(await violations()).toEqual([]);
    await act(async () => within(sheet).getByRole('button', { name: t('ux_close') }).click());
    await act(async () => screen.getByRole('button', { name: t('node_add_more') }).click());
    const add = await screen.findByRole('dialog', { name: t('node_add') });
    await act(async () => within(add).getByRole('button', { name: t('lan_pair_button') }).click());
    await within(add).findByRole('img', { name: t('node_add_qr_alt') });
    expect(await violations()).toEqual([]);
  });

  it('on a paired device', async () => {
    await start('#/settings/privacy', { kind: 'device', token: 'x'.repeat(43), clientId: 'device:a', deviceId: 'a', name: 'Handy' });
    expect(await violations()).toEqual([]);
  });
});

describe('copy', () => {
  // A string missing from a dictionary would show its key; every page is checked in both languages.
  const KEY = /\b(?:ux|perm|caps|settings|stream|home|day|presets|lan|device|node|privacy|mode)_[a-z0-9_]+\b/;
  it.each(['de', 'en'] as const)('shows no dictionary key on any page (%s)', async (lang) => {
    for (const hash of ['#/today', '#/nox/talk', '#/nox/history', '#/devices/devices', '#/devices/skills', '#/settings', '#/settings/privacy', '#/settings/voice', '#/settings/system']) {
      await start(hash, { kind: 'local', token: 'tok' }, lang);
      expect(document.body.textContent ?? '', `${lang} ${hash}`).not.toMatch(KEY);
      cleanup();
    }
  });
});
