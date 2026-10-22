/**
 * The computer-use banner: it follows `desktop.computer_use` on every page, has no close button,
 * and is gone again when the session ends or the core disconnects.
 */

import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Auth } from '../auth';
import type { DashboardIpcHandlers, IpcClient } from '../ipc';
import { DASHBOARD_PATTERNS } from '../ipc';
import { translator } from '../i18n';
import { INITIAL_STATE, parseComputerUse, reduceEvent, setConnected } from '../model';

const captured: { handlers: DashboardIpcHandlers | null } = { handlers: null };

vi.mock('../ipc', async (importOriginal) => {
  const original = await importOriginal<typeof import('../ipc')>();
  return {
    ...original,
    createDashboardClient: (_auth: Auth, handlers: DashboardIpcHandlers) => {
      captured.handlers = handlers;
      return Promise.resolve({
        close: () => undefined,
        request: () => new Promise(() => undefined),
      } as unknown as IpcClient);
    },
  };
});

// Imported after the mock is declared (vitest hoists `vi.mock` above the imports anyway).
const { App } = await import('../App');

const t = translator('de');
const LOCAL: Auth = { kind: 'local', token: 'tok' };

function emit(payload: Record<string, unknown>) {
  act(() => {
    captured.handlers?.onEvent({
      v: 1,
      id: 'e1',
      kind: 'event',
      name: 'desktop.computer_use',
      ts: '2026-10-19T10:00:00Z',
      source: 'desktop',
      payload,
    } as never);
  });
}

describe('parseComputerUse', () => {
  it('reads the event and refuses a payload without a boolean `active`', () => {
    expect(parseComputerUse({ active: true, reason: 'chat', stop: 'ctrl+alt+shift+k' })).toEqual({
      active: true,
      reason: 'chat',
      stop: 'ctrl+alt+shift+k',
      device: '',
      deviceId: '',
    });
    expect(parseComputerUse({ active: 'yes' })).toBeNull();
    expect(parseComputerUse(null)).toBeNull();
  });

  it('is forgotten on disconnect', () => {
    let s = setConnected(INITIAL_STATE, true);
    s = reduceEvent(s, 'desktop.computer_use', { active: true, reason: '', stop: 'f12' });
    expect(s.computerUse?.active).toBe(true);
    expect(setConnected(s, false).computerUse).toBeNull();
  });

  it('is subscribed by name, not as the whole desktop namespace', () => {
    expect(DASHBOARD_PATTERNS).toContain('desktop.computer_use');
    expect(DASHBOARD_PATTERNS).not.toContain('desktop.*');
  });
});

describe('the computer-use strip in the app', () => {
  beforeEach(() => {
    captured.handlers = null;
  });

  it('appears while a session runs and disappears when it ends', async () => {
    render(<App auth={LOCAL} lang="de" theme="system" />);
    await vi.waitFor(() => expect(captured.handlers).not.toBeNull());
    expect(screen.queryByText(/steuert gerade Maus und Tastatur/)).toBeNull();

    emit({ active: true, reason: 'user asked', stop: 'ctrl+alt+shift+k' });
    const banner = screen.getByText(/steuert gerade Maus und Tastatur/);
    expect(banner.closest('[role="alert"]')).not.toBeNull();
    expect(banner.textContent).toBe(
      'Nox steuert gerade Maus und Tastatur – Strg + Alt + Umschalt + K beendet sofort.',
    );
    // Nothing on or around it closes it.
    expect(screen.queryByRole('button', { name: /schließen|ausblenden/i })).toBeNull();

    emit({ active: false, reason: 'stop hotkey', stop: 'ctrl+alt+shift+k' });
    expect(screen.queryByText(/steuert gerade Maus und Tastatur/)).toBeNull();
  });

  it('stays on screen whichever section is open', async () => {
    render(<App auth={LOCAL} lang="de" theme="system" />);
    await vi.waitFor(() => expect(captured.handlers).not.toBeNull());
    emit({ active: true, reason: '', stop: '' });
    const nav = screen.getAllByRole('link', { name: t('ux_area_devices') })[0]!;
    await act(async () => nav.click());
    expect(nav.getAttribute('aria-current')).toBe('page');
    expect(screen.getByText(t('computer_use_banner_no_hotkey')).closest('[role="alert"]')).not.toBeNull();
  });

  it('names the paired PC being controlled, with a Stopp, apart from this machine', async () => {
    render(<App auth={LOCAL} lang="de" theme="system" />);
    await vi.waitFor(() => expect(captured.handlers).not.toBeNull());
    const node = { reason: 'for 15 minutes', stop: 'ctrl+alt+shift+k', device: 'Arbeits-PC', device_id: 'a'.repeat(32) };
    emit({ active: true, ...node });
    const line = screen.getByText(/steuert gerade Maus und Tastatur auf Arbeits-PC/);
    const strip = line.closest('[role="alert"]')!;
    expect(strip).not.toBeNull();
    expect(line.textContent).toContain('Strg + Alt + Umschalt + K');
    // The stop in the line is the same `security.kill` as the header's Stopp.
    expect(strip.querySelector('button.stop')?.textContent).toContain(t('ux_stop'));
    // This machine's own session is a line of its own; ending it leaves the PC's line.
    emit({ active: true, reason: '', stop: 'f12' });
    emit({ active: false, reason: 'done', stop: 'f12' });
    expect(screen.getByText(/auf Arbeits-PC/)).not.toBeNull();
    emit({ active: false, ...node, reason: 'kill switch' });
    expect(screen.queryByText(/auf Arbeits-PC/)).toBeNull();
  });

  it('keeps one line per PC and forgets them on disconnect', () => {
    let s = setConnected(INITIAL_STATE, true);
    s = reduceEvent(s, 'desktop.computer_use', { active: true, device: 'Laptop', device_id: 'b1', stop: '' });
    s = reduceEvent(s, 'desktop.computer_use', { active: true, device: 'Laptop', device_id: 'b1', stop: '' });
    s = reduceEvent(s, 'desktop.computer_use', { active: true, device: 'Arbeits-PC', device_id: 'c2', stop: '' });
    expect(s.nodeComputerUse.map((n) => n.device)).toEqual(['Laptop', 'Arbeits-PC']);
    expect(s.computerUse).toBeNull();
    expect(setConnected(s, false).nodeComputerUse).toEqual([]);
  });

  it('ignores a malformed event', async () => {
    render(<App auth={LOCAL} lang="de" theme="system" />);
    await vi.waitFor(() => expect(captured.handlers).not.toBeNull());
    emit({ active: 'true' });
    expect(screen.queryByText(/steuert gerade Maus und Tastatur/)).toBeNull();
  });
});
