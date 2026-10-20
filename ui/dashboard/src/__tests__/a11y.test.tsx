/**
 * axe-core on the shell and every area (Spec UX-Neubau §10: WCAG 2.2 AA, zero findings).
 *
 * jsdom has no layout, so the two rules that need one - colour contrast and target size - are
 * measured elsewhere: contrast by `tokens.test.ts` on the palette itself, and both by the headless
 * click tests (`tests/e2e/test_ux_redesign_stubbed.py`), which run axe in a real browser at three
 * sizes in light and dark.
 */

import { act, render, screen, within } from '@testing-library/react';
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

async function start(hash: string, auth: Auth = { kind: 'local', token: 'tok' }) {
  window.history.replaceState(null, '', hash);
  await act(async () => {
    render(<App auth={auth} lang="de" theme="system" />);
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

  it('while stopped, and with a question as a sheet', async () => {
    await start('#/settings');
    await emit('security.kill_switch', { origin: 'ui' });
    expect(await violations()).toEqual([]);
    await emit('security.permission_requested', { ...prompt, request_id: 'p2' });
    await screen.findByRole('alertdialog');
    expect(await violations()).toEqual([]);
  });

  it('on a paired device', async () => {
    await start('#/settings/privacy', { kind: 'device', token: 'x'.repeat(43), clientId: 'device:a', deviceId: 'a', name: 'Handy' });
    expect(await violations()).toEqual([]);
  });
});
