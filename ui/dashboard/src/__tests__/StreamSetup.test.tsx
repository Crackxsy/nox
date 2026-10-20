/**
 * Stream page, "Set up": the overlay and the audio track. What matters is that a failed or declined
 * setup is said as such, and that the overlay credential never shows up on the page.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import { type IpcClient } from '../ipc';
import { StreamSetup } from '../pages/StreamSetup';

const t = translator('en');

type Sent = { name: string; payload: unknown };

function fakeClient(answers: Record<string, unknown>): { client: IpcClient; sent: Sent[] } {
  const sent: Sent[] = [];
  const client = {
    request: (name: string, payload: Record<string, unknown>) => {
      sent.push({ name, payload });
      return Promise.resolve(answers[name] ?? { ok: true });
    },
  } as unknown as IpcClient;
  return { client, sent };
}

async function renderSetup(client: IpcClient) {
  await act(async () => {
    render(<StreamSetup t={t} lang="en" client={client} />);
  });
}

describe('Stream setup', () => {
  it('sets up the overlay and names where it went, never the credential', async () => {
    const { client, sent } = fakeClient({
      'stream.overlay.status': { available: true, configured: false },
      'stream.overlay.setup': {
        ok: true,
        input_name: 'Nox Overlay',
        scene: 'Live',
        url: 'http://127.0.0.1:47801/pet/?overlay=1#overlay=<hidden>',
      },
    });
    await renderSetup(client);
    await userEvent.click(screen.getByRole('button', { name: t('stream_overlay_setup') }));
    await waitFor(() => expect(screen.getByText(/"Nox Overlay" in the scene "Live"/)).toBeTruthy());
    expect(sent.map((s) => s.name)).toContain('stream.overlay.setup');
    expect(document.body.textContent).not.toContain('#overlay=');
  });

  it('says so when the overlay was not set up', async () => {
    const { client } = fakeClient({
      'stream.overlay.status': { available: true, configured: false },
      'stream.overlay.setup': { ok: false, error: 'permission.denied' },
    });
    await renderSetup(client);
    await userEvent.click(screen.getByRole('button', { name: t('stream_overlay_setup') }));
    await waitFor(() => expect(screen.getByText(/Not set up: permission.denied/)).toBeTruthy());
  });

  it('offers revoking once an overlay exists', async () => {
    const { client, sent } = fakeClient({
      'stream.overlay.status': {
        available: true,
        configured: true,
        credential: { id: 'abc', label: 'obs', created_at: '2026-10-19T12:00:00+00:00' },
      },
    });
    await renderSetup(client);
    await userEvent.click(await screen.findByRole('button', { name: t('stream_overlay_revoke') }));
    await waitFor(() => expect(sent.map((s) => s.name)).toContain('stream.overlay.revoke'));
  });

  it('sends the chosen tracks and reports a missing virtual cable', async () => {
    const { client, sent } = fakeClient({
      'stream.overlay.status': { available: true, configured: false },
      'stream.audio_track.setup': { ok: false, reason: 'no virtual audio cable found' },
    });
    await renderSetup(client);
    await userEvent.click(screen.getByRole('checkbox', { name: 'Track 3' }));
    await userEvent.click(screen.getByRole('button', { name: t('stream_audio_setup') }));
    await waitFor(() => expect(screen.getByText(/no virtual audio cable found/)).toBeTruthy());
    expect(sent.find((s) => s.name === 'stream.audio_track.setup')?.payload).toEqual({ tracks: [3] });
  });
});
