/**
 * The Presets tab: what it shows, and the two things it must never do.
 *
 * Never: report a run as successful when a step failed, and swallow a rejection from the core
 * when a preset is saved. Both are the same mistake in different clothes - a screen that looks
 * like it worked is worse than one that plainly did not, because the user only finds out when
 * the phrase does nothing.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import { type IpcClient } from '../ipc';
import { PresetsPage } from '../pages/Presets';

const t = translator('de');

const LISTING = {
  enabled: true,
  presets: [
    {
      id: 'gaming',
      name: 'Gaming',
      enabled: true,
      triggers: {
        phrases: ['gaming mode'],
        at: '',
        days: [],
        on_process_start: 'RocketLeague.exe',
        on_process_end: '',
      },
      steps: [
        { kind: 'light', entity_ids: ['light.desk'], brightness_pct: 30 },
        { kind: 'run', action: 'dpi_low' },
      ],
    },
  ],
  actions: [
    {
      id: 'dpi_low',
      name: 'DPI 800',
      command: ['C:/Tools/ahk.exe', 'dpi.ahk'],
      working_dir: '',
      timeout_s: 20,
      report_failure: true,
    },
  ],
};

type Sent = { name: string; payload: unknown };

function fakeClient(overrides: Record<string, unknown> = {}): { client: IpcClient; sent: Sent[] } {
  const sent: Sent[] = [];
  const client = {
    request: (name: string, payload: Record<string, unknown>) => {
      sent.push({ name, payload });
      if (name in overrides) return Promise.resolve(overrides[name]);
      if (name === 'presets.list') return Promise.resolve(LISTING);
      return Promise.resolve({ ok: true });
    },
  } as unknown as IpcClient;
  return { client, sent };
}

async function renderPage(client: IpcClient | null) {
  await act(async () => {
    render(<PresetsPage t={t} client={client} />);
  });
}

describe('Presets page', () => {
  it('lists a preset with its phrase and its steps', async () => {
    const { client } = fakeClient();

    await renderPage(client);

    expect(await screen.findByText('Gaming')).toBeTruthy();
    expect(screen.getByText(/gaming mode/)).toBeTruthy();
    expect(screen.getByText('Licht · Programm starten')).toBeTruthy();
  });

  it('says so when nothing is configured yet', async () => {
    const { client } = fakeClient({ 'presets.list': { enabled: true, presets: [], actions: [] } });

    await renderPage(client);

    expect(await screen.findByText(t('presets_none'))).toBeTruthy();
  });

  it('names the step that failed instead of claiming the run worked', async () => {
    const { client } = fakeClient({
      'presets.activate': {
        preset_id: 'gaming',
        name: 'Gaming',
        trigger: 'manual',
        ok: false,
        duration_ms: 12,
        steps: [
          { index: 1, kind: 'light', ok: false, error: 'die Lampe antwortet nicht', duration_ms: 5 },
          { index: 2, kind: 'run', ok: true, error: '', duration_ms: 7 },
        ],
      },
    });
    await renderPage(client);

    await userEvent.click(await screen.findByRole('button', { name: t('presets_run') }));

    await waitFor(() => {
      expect(screen.getByText(t('presets_run_partial'))).toBeTruthy();
    });
    expect(screen.getByText(/die Lampe antwortet nicht/)).toBeTruthy();
  });

  it('reports a program that could not be started', async () => {
    const { client } = fakeClient({
      'presets.test_action': {
        ok: false,
        exit_code: null,
        error: "the program for 'DPI 800' was not found",
        duration_ms: 3,
        timed_out: false,
      },
    });
    await renderPage(client);

    await userEvent.click(await screen.findByRole('button', { name: t('presets_action_test') }));

    await waitFor(() => {
      expect(screen.getByText(t('presets_action_failed'))).toBeTruthy();
    });
    expect(screen.getByText(/was not found/)).toBeTruthy();
  });

  it('saves through config.set so the core validates the whole section', async () => {
    const { client, sent } = fakeClient({ 'config.set': { ok: true, errors: {} } });
    await renderPage(client);

    const [editPreset] = await screen.findAllByRole('button', { name: t('presets_edit') });
    await userEvent.click(editPreset!);
    await userEvent.click(screen.getByRole('button', { name: t('presets_save') }));

    await waitFor(() => {
      expect(sent.some((entry) => entry.name === 'config.set')).toBe(true);
    });
    const write = sent.find((entry) => entry.name === 'config.set');
    // The key is the literal dotted path, not a nested object, so it is looked up as one.
    const values = (write?.payload as { values: Record<string, unknown> }).values;
    expect(Object.keys(values)).toContain('presets.items');
  });

  it('shows the reason when the core refuses a preset', async () => {
    const { client } = fakeClient({
      'config.set': { ok: false, errors: { 'presets.items': "unknown action 'ghost'" } },
    });
    await renderPage(client);

    const [editPreset] = await screen.findAllByRole('button', { name: t('presets_edit') });
    await userEvent.click(editPreset!);
    await userEvent.click(screen.getByRole('button', { name: t('presets_save') }));

    await waitFor(() => {
      expect(screen.getByText(/unknown action 'ghost'/)).toBeTruthy();
    });
  });
});
