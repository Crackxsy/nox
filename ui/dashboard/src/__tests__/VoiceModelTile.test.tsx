/**
 * "Stimme": what is missing and how big it is, the license note, a download the tile polls to the
 * end, and the honest refusals.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import type { IpcClient } from '../ipc';
import { canDownloadVoiceModel, parseVoiceModelStatus, voiceModelFraction } from '../model';
import { VOICE_MODEL_POLL_MS, VoiceModelTile } from '../pages/settings/VoiceModelTile';

const t = translator('de');

const MISSING = {
  engine: 'supertonic',
  configured_engine: 'supertonic',
  directory: 'C:/Nox/models/supertonic',
  files: 17,
  missing: Array.from({ length: 17 }, (_, i) => `f${i}`),
  present: false,
  size_bytes: 401_000_000,
  missing_bytes: 401_000_000,
  license: 'OpenRAIL-M',
  running: false,
  outcome: null,
  detail: '',
  progress: null,
  privacy_mode: 'balanced',
  download_blocked: false,
  restart_required: false,
};

const RUNNING = {
  ...MISSING,
  running: true,
  progress: { file: 'onnx/vocoder.onnx', done: 100_250_000, total: 401_000_000, index: 3, count: 17 },
};

const DONE = {
  ...MISSING,
  missing: [],
  present: true,
  missing_bytes: 0,
  outcome: 'done',
  restart_required: true,
};

function fakeClient(replies: Record<string, unknown[]>) {
  const sent: string[] = [];
  const client = {
    request: async (name: string) => {
      sent.push(name);
      const queue = replies[name] ?? [];
      return queue.length > 1 ? queue.shift() : queue[0];
    },
  } as unknown as IpcClient;
  return { client, sent };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('parseVoiceModelStatus', () => {
  it('keeps what the core said and nothing else', () => {
    const status = parseVoiceModelStatus(RUNNING);
    expect(status.missing).toHaveLength(17);
    expect(voiceModelFraction(status.progress)).toBeCloseTo(0.25);
    expect(parseVoiceModelStatus('nonsense').files).toBe(0);
    expect(voiceModelFraction(null)).toBeNull();
  });

  it('knows when "Herunterladen" can do something', () => {
    expect(canDownloadVoiceModel(parseVoiceModelStatus(MISSING))).toBe(true);
    expect(canDownloadVoiceModel(parseVoiceModelStatus(RUNNING))).toBe(false);
    expect(canDownloadVoiceModel(parseVoiceModelStatus(DONE))).toBe(false);
    expect(
      canDownloadVoiceModel(parseVoiceModelStatus({ ...MISSING, download_blocked: true })),
    ).toBe(false);
  });
});

describe('VoiceModelTile', () => {
  it('shows the missing files, the size and the license note before anything downloads', async () => {
    const { client, sent } = fakeClient({ 'voice.model.status': [MISSING] });
    render(<VoiceModelTile t={t} lang="de" client={client} />);
    await waitFor(() => expect(screen.getByText('17 von 17 fehlen')).toBeTruthy());
    expect(screen.getByText('etwa 401 MB')).toBeTruthy();
    expect(screen.getByText('OpenRAIL-M')).toBeTruthy();
    expect(screen.getByText(t('voicemodel_license_openrail'))).toBeTruthy();
    expect(sent).toEqual(['voice.model.status']);
  });

  it('downloads on click, polls the progress and says when it applies', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { client, sent } = fakeClient({
      'voice.model.status': [MISSING, DONE],
      'voice.model.download': [RUNNING],
    });
    render(<VoiceModelTile t={t} lang="de" client={client} />);
    const button = await screen.findByRole('button', { name: t('voicemodel_download') });
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByText(t('voicemodel_running'))).toBeTruthy());
    expect(screen.getByRole('progressbar', { name: t('voicemodel_progress_label') })).toBeTruthy();
    expect(screen.getByText(/25 % von 401 MB – Datei 3 von 17/)).toBeTruthy();
    // Poll ticks until the finished state is on screen; a busy CI machine may need more than one
    // before the interval effect is in place.
    for (let tick = 0; tick < 10 && !screen.queryByText(t('voicemodel_out_done')); tick += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(VOICE_MODEL_POLL_MS);
      });
    }
    await waitFor(() => expect(screen.getByText(t('voicemodel_out_done'))).toBeTruthy());
    expect(sent.slice(0, 3)).toEqual([
      'voice.model.status',
      'voice.model.download',
      'voice.model.status',
    ]);
    expect(sent.slice(1).filter((name) => name === 'voice.model.download')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: t('voicemodel_download') })).toBeNull();
  });

  it('explains a privacy mode that forbids the download and disables the button', async () => {
    const blocked = { ...MISSING, privacy_mode: 'offline', download_blocked: true };
    const { client } = fakeClient({ 'voice.model.status': [blocked] });
    render(<VoiceModelTile t={t} lang="de" client={client} />);
    const button = await screen.findByRole('button', { name: t('voicemodel_download') });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/lädt Nox nichts herunter/)).toBeTruthy();
  });

  it('shows a failure in the core’s words', async () => {
    const failed = { ...MISSING, outcome: 'failed', detail: 'onnx/vocoder.onnx: ConnectError' };
    const { client } = fakeClient({ 'voice.model.status': [failed] });
    render(<VoiceModelTile t={t} lang="de" client={client} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('ConnectError');
    expect(alert.textContent).toContain('Piper');
  });

  it('says which engine is selected when it is not Supertonic', async () => {
    const piper = { ...MISSING, configured_engine: 'piper' };
    const { client } = fakeClient({ 'voice.model.status': [piper] });
    render(<VoiceModelTile t={t} lang="en" client={client} />);
    await waitFor(() => expect(screen.getByText(/Eingestellt ist Piper/)).toBeTruthy());
  });
});
