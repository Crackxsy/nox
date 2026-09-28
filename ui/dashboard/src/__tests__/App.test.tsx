/**
 * The dashboard shell when the core restarts underneath it. The page holds a session token that
 * the new core no longer accepts; the page cannot fetch the new one, so it has to say how to get
 * back - once, clearly, without the misleading "offline, last known values" banner and without a
 * retry storm (the client itself stops; see shared/__tests__/session.test.ts).
 */

import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../App';
import { translator } from '../i18n';
import type { ConnStatus, IpcClient } from '../ipc';

const t = translator('de');

let reportStatus: (status: ConnStatus, detail?: string) => void = () => undefined;

vi.mock('../ipc', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../ipc')>();
  return {
    ...actual,
    createDashboardClient: vi.fn(
      async (_token: string, handlers: { onStatus: (s: ConnStatus, d?: string) => void }) => {
        reportStatus = handlers.onStatus;
        return {
          status: 'connecting',
          close: () => undefined,
          request: () => Promise.resolve({}),
        } as unknown as IpcClient;
      },
    ),
  };
});

beforeEach(() => {
  reportStatus = () => undefined;
});

async function renderApp() {
  render(<App token="old-token" lang="de" theme="system" />);
  await act(async () => {
    await Promise.resolve();
  });
}

describe('after a core restart', () => {
  it('says Nox was restarted and how to reopen the dashboard', async () => {
    await renderApp();
    act(() => reportStatus('online'));
    act(() => reportStatus('session_expired', 'invalid token'));

    const alerts = screen.getAllByRole('alert').map((a) => a.textContent);
    expect(alerts).toContain(t('session_expired'));
    expect(alerts).not.toContain(t('offline_hint'));
    expect(screen.getByText(t('conn_session_expired'))).toBeDefined();
  });

  it('a token refused from the start points to the tray as well', async () => {
    await renderApp();
    act(() => reportStatus('auth_failed', 'invalid token'));
    const alerts = screen.getAllByRole('alert').map((a) => a.textContent);
    expect(alerts).toContain(t('auth_failed_hint'));
    expect(alerts).not.toContain(t('offline_hint'));
  });

  it('a plain disconnect still says the values are the last known ones', async () => {
    await renderApp();
    act(() => reportStatus('online'));
    act(() => reportStatus('offline'));
    expect(screen.getAllByRole('alert').map((a) => a.textContent)).toContain(t('offline_hint'));
  });
});
