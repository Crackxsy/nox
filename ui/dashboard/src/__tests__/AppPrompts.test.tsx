/**
 * The shell's side of permission prompts and paired devices: the questions still open after a
 * (re)connect, how a question that ended elsewhere is reported, the page behind a question, and
 * what a refused device does with its pairing.
 */

import { act, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Auth } from '../auth';
import type { DashboardIpcHandlers } from '../ipc';

const harness = vi.hoisted(() => ({
  handlers: null as DashboardIpcHandlers | null,
  answers: {} as Record<string, unknown>,
  calls: [] as string[],
}));

vi.mock('../ipc', async (importOriginal) => {
  const real = await importOriginal<typeof import('../ipc')>();
  return {
    ...real,
    createDashboardClient: vi.fn(async (_auth: Auth, handlers: DashboardIpcHandlers) => {
      harness.handlers = handlers;
      return {
        request: (name: string) => {
          harness.calls.push(name);
          return Promise.resolve(harness.answers[name] ?? {});
        },
        close: () => undefined,
      };
    }),
  };
});

const { App } = await import('../App');
const { DASHBOARD_PATTERNS, SIGNED_OUT_DETAIL } = await import('../ipc');
const { fill, translator } = await import('../i18n');

const t = translator('de');
const LOCAL: Auth = { kind: 'local', token: 'tok' };
const DEVICE: Auth = { kind: 'device', token: 'x'.repeat(43), clientId: 'device:abc', deviceId: 'abc', name: 'Tablet' };

const requested = (id: string) => ({
  request_id: id,
  agent: 'companion',
  tool: 'file',
  action: 'delete',
  mode: 'companion',
  risk: 'medium',
  target: `Archiv/${id}.zip`,
  expires_at: new Date(Date.now() + 60_000).toISOString(),
  remember_ttl_s: 28800,
});

function event(name: string, payload: Record<string, unknown>) {
  return { v: 1, id: `e-${Math.random()}`, ts: new Date().toISOString(), kind: 'event' as const, name, corr: null, src: { role: 'core' as const, id: 'core' }, payload };
}

async function start(auth: Auth, onDeviceGone = vi.fn(), hash = '#/devices/devices') {
  // A page that does not show questions inline: the question is a sheet there.
  window.history.replaceState(null, '', hash);
  await act(async () => {
    render(<App auth={auth} lang="de" theme="system" onDeviceGone={onDeviceGone} />);
  });
  return onDeviceGone;
}

async function status(s: 'online' | 'offline' | 'auth_failed', detail?: string) {
  await act(async () => {
    harness.handlers!.onStatus(s, detail);
  });
}

async function emit(name: string, payload: Record<string, unknown>) {
  await act(async () => {
    harness.handlers!.onEvent(event(name, payload));
  });
}

beforeEach(() => {
  harness.handlers = null;
  harness.answers = {};
  harness.calls = [];
});

afterEach(() => {
  vi.useRealTimers();
});

describe('questions after a (re)connect', () => {
  it('shows the questions still open, and drops stale ones on the next connect', async () => {
    harness.answers['security.permission.pending'] = { pending: [requested('a')] };
    await start(LOCAL);
    await status('online');
    expect((await screen.findByRole('alertdialog', { name: t('ux_perm_q_delete') })).textContent).toContain('Archiv/a.zip');

    // Answered elsewhere while this page was away: the core no longer lists it.
    await status('offline');
    harness.answers['security.permission.pending'] = { pending: [] };
    await status('online');
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(harness.calls.filter((c) => c === 'security.permission.pending')).toHaveLength(2);
  });

  it('makes the page behind a question inert, and gives it back afterwards', async () => {
    await start(LOCAL);
    await status('online');
    await emit('security.permission_requested', requested('b'));
    const app = document.querySelector('.app')!;
    expect(app.hasAttribute('inert')).toBe(true);
    expect((await screen.findByRole('alertdialog')).closest('.app')).toBeNull();
    await emit('security.permission_decided', { request_id: 'b', decision: 'allow', rule_id: 'confirm.user', by: 'device:x' });
    expect(app.hasAttribute('inert')).toBe(false);
  });
});

describe('the question where it belongs', () => {
  it('sits inline under "Wartet auf dich" on Heute, and nothing behind it is inert', async () => {
    await start(LOCAL, vi.fn(), '#/today');
    await status('online');
    await emit('security.permission_requested', requested('h'));
    const card = await screen.findByRole('region', { name: t('ux_perm_q_delete') });
    expect(card.closest('#waiting')).not.toBeNull();
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(document.querySelector('.app')!.hasAttribute('inert')).toBe(false);
    expect(card.textContent).toContain(fill(t('ux_perm_risk'), t('perm_risk_medium')));
  });

  it('says "Erlaubt." after this page allowed it', async () => {
    harness.answers['security.permission.reply'] = { ok: true };
    await start(LOCAL, vi.fn(), '#/today');
    await status('online');
    await emit('security.permission_requested', requested('i'));
    const card = await screen.findByRole('region', { name: t('ux_perm_q_delete') });
    const allow = within(card).getByRole('button', { name: t('perm_allow') }) as HTMLButtonElement;
    // A new question's "Erlauben" waits a moment (no double click allows it unseen).
    expect(allow.disabled).toBe(true);
    await waitFor(() => expect(allow.disabled).toBe(false), { timeout: 2000 });
    await act(async () => {
      allow.click();
    });
    expect(harness.calls).toContain('security.permission.reply');
    expect(screen.getByText(t('ux_perm_allowed'))).toBeTruthy();
  });

  it('is cancelled by Stopp, and says so', async () => {
    await start(LOCAL, vi.fn(), '#/today');
    await status('online');
    await emit('security.permission_requested', requested('j'));
    await screen.findByRole('region', { name: t('ux_perm_q_delete') });
    await emit('security.kill_switch', { origin: 'ui' });
    await emit('security.permission_decided', { request_id: 'j', decision: 'deny', rule_id: 'kill', by: 'policy' });
    expect(screen.getByText(t('ux_perm_stopped'))).toBeTruthy();
  });
});

describe('how a question ended without this page', () => {
  it('says nobody answered when the core gave up', async () => {
    await start(LOCAL);
    await status('online');
    await emit('security.permission_requested', requested('c'));
    await emit('security.permission_decided', { request_id: 'c', decision: 'deny', rule_id: 'confirm.timeout', by: 'policy' });
    expect(screen.getByText('Keine Antwort – Nox hat es nicht getan.')).toBeTruthy();
  });

  it('says another device decided when one did', async () => {
    await start(LOCAL);
    await status('online');
    await emit('security.permission_requested', requested('d'));
    await emit('security.permission_decided', { request_id: 'd', decision: 'allow', rule_id: 'confirm.user', by: 'device:x' });
    expect(screen.getByText(t('perm_decided_elsewhere'))).toBeTruthy();
  });

  it('says it as a toast at the top right that takes no clicks and hides by itself', async () => {
    await start(LOCAL);
    await status('online');
    await emit('security.permission_requested', requested('h'));
    vi.useFakeTimers();
    await emit('security.permission_decided', { request_id: 'h', decision: 'allow', rule_id: 'confirm.user', by: 'device:x' });
    const note = document.querySelector('.perm-note')!;
    expect(note.textContent).toBe(t('perm_decided_elsewhere'));
    expect(note.closest('.toast-stack')).not.toBeNull();
    expect(note.closest('.app')).toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(6000);
    });
    expect(note.textContent).toBe('');
    vi.useRealTimers();
  });

  it('says nothing about a question it never showed', async () => {
    await start(LOCAL);
    await status('online');
    await emit('security.permission_decided', { request_id: 'zzz', decision: 'deny', rule_id: 'confirm.timeout', by: 'policy' });
    expect(document.querySelector('.perm-note')!.textContent).toBe('');
  });
});

describe('a paired device the core refuses', () => {
  it('keeps its pairing when network access is off, says so and tries again', async () => {
    const gone = await start(DEVICE);
    await status('auth_failed', 'network access is off');
    expect(screen.getByText(t('device_lan_off'))).toBeTruthy();
    expect(screen.getByRole('button', { name: t('device_reconnect') })).toBeTruthy();
    expect(document.body.textContent).not.toContain('network access is off');
    expect(gone).not.toHaveBeenCalled();
  });

  it('forgets it only when it was removed or belongs to another device', async () => {
    const gone = await start(DEVICE);
    await status('auth_failed', 'unknown or revoked device');
    expect(gone).toHaveBeenCalledWith('removed');
  });

  it('says "signed out" when its session was closed for that', async () => {
    const gone = await start(DEVICE);
    await status('auth_failed', SIGNED_OUT_DETAIL);
    expect(gone).toHaveBeenCalledWith('signed_out');
  });
});

describe('what a dashboard subscribes to', () => {
  it('gets pairing news but no raw Telegram traffic', () => {
    expect(DASHBOARD_PATTERNS).toContain('remote.paired');
    expect(DASHBOARD_PATTERNS).toContain('remote.revoked');
    expect(DASHBOARD_PATTERNS).not.toContain('remote.*');
  });
});
