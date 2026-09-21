/**
 * The kill switch, as a component.
 *
 * This is the test the repository did not have and needed most: the shipped page turned into a
 * *one-click* kill switch the moment it had been used once, because `killNext('sent', 'press')`
 * answered `'sent'` and the page read that as "fire now". Nothing caught it, because vitest was
 * restricted to `environment: 'node'` and `*.test.ts`, so no `.tsx` file could be tested at all.
 *
 * What is pinned here: one press never fires, two presses fire exactly one request, a third press
 * fires nothing, cancelling disarms, and the armed button is not the ordinary primary button.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import type { IpcClient } from '../ipc';
import { INITIAL_STATE, type DashboardState } from '../model';
import { StatusPage } from '../pages/Status';

const t = translator('de');

function fakeClient(): { client: IpcClient; sent: { name: string; payload: unknown }[] } {
  const sent: { name: string; payload: unknown }[] = [];
  const client = {
    request: (name: string, payload: Record<string, unknown>) => {
      sent.push({ name, payload });
      return Promise.resolve({ ok: true });
    },
  } as unknown as IpcClient;
  return { client, sent };
}

function renderStatus(state: Partial<DashboardState> = {}) {
  const { client, sent } = fakeClient();
  render(
    <StatusPage
      t={t}
      lang="de"
      state={{ ...INITIAL_STATE, connected: true, ...state }}
      client={client}
      providersFailed={false}
      onRefresh={() => undefined}
    />,
  );
  return { sent };
}

describe('kill switch', () => {
  it('needs two presses and never fires on the first one', () => {
    const { sent } = renderStatus();
    fireEvent.click(screen.getByRole('button', { name: t('kill_button') }));
    expect(sent).toHaveLength(0);
    expect(screen.getByRole('button', { name: t('kill_confirm_button') })).toBeDefined();
  });

  it('fires exactly one security.kill on the second press', () => {
    const { sent } = renderStatus();
    fireEvent.click(screen.getByRole('button', { name: t('kill_button') }));
    fireEvent.click(screen.getByRole('button', { name: t('kill_confirm_button') }));
    expect(sent.map((s) => s.name)).toEqual(['security.kill']);
  });

  it('does not fire again once it has been used', () => {
    const { sent } = renderStatus();
    fireEvent.click(screen.getByRole('button', { name: t('kill_button') }));
    fireEvent.click(screen.getByRole('button', { name: t('kill_confirm_button') }));
    const after = screen.getByRole('button', { name: t('kill_button') });
    expect(after.hasAttribute('disabled')).toBe(true);
    fireEvent.click(after);
    fireEvent.click(after);
    expect(sent).toHaveLength(1);
  });

  it('says so, and stays disabled, after a kill', () => {
    renderStatus();
    fireEvent.click(screen.getByRole('button', { name: t('kill_button') }));
    fireEvent.click(screen.getByRole('button', { name: t('kill_confirm_button') }));
    expect(screen.getByText(t('kill_sent'))).toBeDefined();
  });

  it('cancelling disarms it without sending anything', () => {
    const { sent } = renderStatus();
    fireEvent.click(screen.getByRole('button', { name: t('kill_button') }));
    fireEvent.click(screen.getByRole('button', { name: t('kill_cancel') }));
    expect(sent).toHaveLength(0);
    expect(screen.getByRole('button', { name: t('kill_button') }).hasAttribute('disabled')).toBe(
      false,
    );
  });

  it('the armed button carries its own class, not the ordinary primary one', () => {
    renderStatus();
    fireEvent.click(screen.getByRole('button', { name: t('kill_button') }));
    const armed = screen.getByRole('button', { name: t('kill_confirm_button') });
    expect(armed.className).toContain('btn--armed');
    expect(armed.className).not.toContain('btn--danger');
  });

  it('the armed step expires on its own', () => {
    vi.useFakeTimers();
    try {
      const { sent } = renderStatus();
      fireEvent.click(screen.getByRole('button', { name: t('kill_button') }));
      act(() => {
        vi.advanceTimersByTime(7000);
      });
      expect(sent).toHaveLength(0);
      expect(screen.queryByRole('button', { name: t('kill_confirm_button') })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('is disabled while the core is unreachable', () => {
    render(
      <StatusPage
        t={t}
        lang="de"
        state={INITIAL_STATE}
        client={null}
        providersFailed={false}
        onRefresh={() => undefined}
      />,
    );
    expect(screen.getByRole('button', { name: t('kill_button') }).hasAttribute('disabled')).toBe(
      true,
    );
  });

  it('is already disabled when the core reports safe mode', () => {
    renderStatus({ systemLevel: 'safe_mode' });
    expect(screen.getByRole('button', { name: t('kill_button') }).hasAttribute('disabled')).toBe(
      true,
    );
  });
});

describe('the home screen speaks German, not identifiers', () => {
  it('shows the level and the mode as words', () => {
    renderStatus({ systemLevel: 'running', mode: 'companion' });
    expect(screen.getByText('Läuft')).toBeDefined();
    expect(screen.getByText('Begleiter-Modus')).toBeDefined();
    expect(screen.queryByText('RUNNING')).toBeNull();
    expect(screen.queryByText('companion')).toBeNull();
  });

  it('keeps the health component id as a secondary detail, not as the label', () => {
    renderStatus({
      health: {
        components: [{ component: 'ai.rules', status: 'available', reason: 'ok' }],
        generatedAt: null,
      },
    });
    expect(screen.getByText('Regelantworten')).toBeDefined();
    // The id stays on screen — a trainer has to be able to look it up — but it is the small line.
    const id = screen.getByText('ai.rules');
    expect(id.className).toContain('detail-sub');
  });

  it('translates a known English reason and keeps the original underneath', () => {
    renderStatus({
      health: {
        components: [
          { component: 'ai.rules', status: 'available', reason: 'deterministic rules, always available' },
        ],
        generatedAt: null,
      },
    });
    expect(screen.getByText('Regelbasierte Antworten, immer verfügbar')).toBeDefined();
    expect(screen.getByText('deterministic rules, always available')).toBeDefined();
  });

  it('shows the vault folder as a word and the path only as the detail line', () => {
    renderStatus({
      health: {
        components: [{ component: 'vault', status: 'available', reason: 'C:\\Users\\x\\vault' }],
        generatedAt: null,
      },
    });
    expect(screen.getByText('Ordner erreichbar')).toBeDefined();
    expect(screen.getByText('C:\\Users\\x\\vault').className).toContain('detail-sub');
  });

  it('shows an untranslated reason verbatim rather than dropping it', () => {
    renderStatus({
      health: {
        components: [{ component: 'db', status: 'unavailable', reason: 'something new' }],
        generatedAt: null,
      },
    });
    expect(screen.getByText('something new')).toBeDefined();
  });
});

describe('the providers card', () => {
  const providers = [
    {
      id: 'ollama',
      displayName: 'Ollama (llama3.2:3b)',
      local: true,
      roles: ['chat', 'classify'],
      status: 'available',
      reason: 'model llama3.2:3b available',
    },
  ];

  it('names the provider, its roles and its state in German', () => {
    renderStatus({ providers });
    expect(screen.getByText('Ollama (llama3.2:3b)')).toBeDefined();
    expect(screen.getByText('lokal')).toBeDefined();
    expect(screen.getByText('Gespräch, Einordnen')).toBeDefined();
    expect(screen.getByText('verfügbar')).toBeDefined();
    expect(screen.getByText('Modell llama3.2:3b geladen')).toBeDefined();
  });

  it('offers a retry and says the list failed, rather than claiming there are none', () => {
    render(
      <StatusPage
        t={t}
        lang="de"
        state={{ ...INITIAL_STATE, connected: true }}
        client={fakeClient().client}
        providersFailed
        onRefresh={() => undefined}
      />,
    );
    expect(screen.getByText(t('providers_failed'))).toBeDefined();
    expect(screen.getByRole('button', { name: t('providers_retry') })).toBeDefined();
  });
});
