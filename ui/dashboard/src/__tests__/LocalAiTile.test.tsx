/**
 * "Lokale KI": the dry run on connect, the honest refusals, and a run the tile polls to the end.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import type { IpcClient } from '../ipc';
import { canStartSetup, downloadFraction, parseLocalAiStatus } from '../model';
import { LOCAL_AI_POLL_MS, LocalAiTile } from '../pages/settings/LocalAiTile';

const t = translator('de');

const CHECKED = {
  running: false,
  phase: 'finished',
  outcome: 'recommended',
  detail: '',
  hardware: { ram_gb: 32, vram_gb: 6, machine: 'AMD64', pi_generation: null },
  recommendation: {
    model: 'qwen3:4b-instruct',
    reason: 'vram',
    basis: 'vram',
    download_gb: 2.5,
    measured: true,
  },
  ollama: {
    reachable: true,
    installed: true,
    version: '0.12.0',
    has_model: false,
    can_install: false,
    instructions: [],
  },
  privacy_mode: 'balanced',
  pull_blocked: false,
  progress: null,
  probe: null,
  model_written: '',
};

const IDLE = {
  ...CHECKED,
  phase: 'idle',
  outcome: null,
  hardware: null,
  recommendation: null,
  ollama: null,
};

function fakeClient(replies: Record<string, unknown[]>) {
  const sent: [string, unknown][] = [];
  const client = {
    request: async (name: string, payload: Record<string, unknown> = {}) => {
      sent.push([name, payload]);
      const queue = replies[name] ?? [];
      return queue.length > 1 ? queue.shift() : queue[0];
    },
  } as unknown as IpcClient;
  return { client, sent };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('parseLocalAiStatus', () => {
  it('keeps a missing VRAM reading as null instead of inventing zero', () => {
    const status = parseLocalAiStatus({
      ...CHECKED,
      hardware: { ram_gb: 16, vram_gb: null, machine: 'x86_64', pi_generation: null },
    });
    expect(status.hardware?.vramGb).toBeNull();
    expect(status.hardware?.ramGb).toBe(16);
    expect(parseLocalAiStatus('nonsense').phase).toBe('idle');
  });

  it('knows when "Einrichten" can do something', () => {
    expect(canStartSetup(parseLocalAiStatus(CHECKED))).toBe(true);
    expect(canStartSetup(parseLocalAiStatus({ ...CHECKED, pull_blocked: true }))).toBe(false);
    expect(
      canStartSetup(
        parseLocalAiStatus({
          ...CHECKED,
          pull_blocked: true,
          ollama: { ...CHECKED.ollama, has_model: true },
        }),
      ),
    ).toBe(true);
    expect(downloadFraction({ status: 'x', completed: 1, total: 4 })).toBe(0.25);
    expect(downloadFraction({ status: 'x', completed: 0, total: 0 })).toBeNull();
  });
});

describe('LocalAiTile', () => {
  it('runs a dry run on connect and shows the recommendation', async () => {
    const { client, sent } = fakeClient({
      'ai.setup.status': [IDLE],
      'ai.setup.check': [CHECKED],
    });
    render(<LocalAiTile t={t} lang="de" client={client} />);
    await waitFor(() => expect(screen.getByText(/qwen3:4b-instruct/)).toBeTruthy());
    expect(screen.getByText(t('localai_reason_vram'))).toBeTruthy();
    expect(sent.map(([name]) => name)).toEqual(['ai.setup.status', 'ai.setup.check']);
    expect(sent.some(([name]) => name === 'ai.setup.start')).toBe(false);
  });

  it('says it chose by RAM when there is no VRAM reading', async () => {
    const amd = {
      ...CHECKED,
      hardware: { ram_gb: 16, vram_gb: null, machine: 'AMD64', pi_generation: null },
      recommendation: { ...CHECKED.recommendation, reason: 'ram_no_vram_reading', basis: 'ram' },
    };
    const { client } = fakeClient({ 'ai.setup.status': [amd] });
    render(<LocalAiTile t={t} lang="de" client={client} />);
    await waitFor(() =>
      expect(screen.getByText(t('localai_reason_ram_no_vram_reading'))).toBeTruthy(),
    );
    expect(screen.getByText(t('localai_vram_none'))).toBeTruthy();
  });

  it('tells a Raspberry Pi 4 honestly that no model makes sense, and offers no button', async () => {
    const pi = {
      ...CHECKED,
      outcome: 'no_llm',
      hardware: { ram_gb: 8, vram_gb: null, machine: 'aarch64', pi_generation: 4 },
      recommendation: {
        model: null,
        reason: 'no_llm_pi',
        basis: null,
        download_gb: null,
        measured: null,
      },
      ollama: null,
    };
    const { client } = fakeClient({ 'ai.setup.status': [pi] });
    render(<LocalAiTile t={t} lang="de" client={client} />);
    await waitFor(() => expect(screen.getByText(t('localai_reason_no_llm_pi'))).toBeTruthy());
    expect(screen.queryByRole('button', { name: t('localai_setup') })).toBeNull();
  });

  it('disables the download in a privacy mode that forbids it', async () => {
    const offline = { ...CHECKED, privacy_mode: 'offline', pull_blocked: true };
    const { client } = fakeClient({ 'ai.setup.status': [offline] });
    render(<LocalAiTile t={t} lang="de" client={client} />);
    const button = await screen.findByRole('button', { name: t('localai_setup') });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/lädt Nox nichts herunter/)).toBeTruthy();
  });

  it('shows the install commands when Ollama is missing', async () => {
    const missing = {
      ...CHECKED,
      ollama: {
        ...CHECKED.ollama,
        reachable: false,
        installed: false,
        version: '',
        instructions: ['winget install --id Ollama.Ollama -e'],
      },
    };
    const { client } = fakeClient({ 'ai.setup.status': [missing] });
    render(<LocalAiTile t={t} lang="de" client={client} />);
    await waitFor(() =>
      expect(screen.getByText('winget install --id Ollama.Ollama -e')).toBeTruthy(),
    );
    const button = screen.getByRole('button', { name: t('localai_setup') });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it('starts a run and polls it to the end', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const pulling = {
      ...CHECKED,
      running: true,
      phase: 'pull',
      outcome: null,
      progress: { status: 'pulling abc', completed: 500, total: 1000 },
    };
    const done = { ...CHECKED, outcome: 'done', model_written: 'qwen3:4b-instruct' };
    const { client, sent } = fakeClient({
      'ai.setup.status': [CHECKED, done],
      'ai.setup.start': [{ ok: true, ...pulling }],
    });
    render(<LocalAiTile t={t} lang="de" client={client} />);
    fireEvent.click(await screen.findByRole('button', { name: t('localai_setup') }));

    await waitFor(() => expect(screen.getByText(/50 %/)).toBeTruthy());
    expect(sent).toContainEqual(['ai.setup.start', { install_ollama: false }]);
    expect(screen.getByRole('progressbar')).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(LOCAL_AI_POLL_MS);
    });
    await waitFor(() => expect(screen.getByText(/ist eingerichtet und eingetragen/)).toBeTruthy());
  });

  it('names a failed probe in the core’s words', async () => {
    const failed = {
      ...CHECKED,
      outcome: 'probe_failed',
      detail: 'tool: expected desktop.windows',
    };
    const { client } = fakeClient({ 'ai.setup.status': [failed] });
    render(<LocalAiTile t={t} lang="de" client={client} />);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('desktop.windows'));
  });
});
