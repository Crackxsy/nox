/**
 * The two tabs a new user meets with nothing configured.
 *
 * On a shipped install both of them greeted the user with a raw English developer error —
 * `Fehler: role 'dashboard' may not call 'clip.list'` and `Fehler: unknown request
 * 'remote.devices.list'` — and the Remote tab then still offered an enabled "Gerät koppeln" form
 * that produced a second one. These tests pin the designed states that replaced them, and the fact
 * that the core's own wording is kept as a detail rather than thrown away.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import { IpcError, type IpcClient } from '../ipc';
import { INITIAL_STATE } from '../model';
import { ClipsPage } from '../pages/Clips';
import { RemotePage } from '../pages/Remote';

const t = translator('de');

function refusing(error: unknown): IpcClient {
  return {
    request: () => Promise.reject(error),
  } as unknown as IpcClient;
}

describe('the Clips tab when the core refuses clip.list', () => {
  it('explains the refusal in German and keeps the core wording as a detail', async () => {
    const error = new IpcError('permission.denied', "role 'dashboard' may not call 'clip.list'");
    await act(async () => {
      render(
        <ClipsPage
          t={t}
          lang="de"
          state={INITIAL_STATE}
          client={refusing(error)}
          onState={() => undefined}
        />,
      );
    });
    await waitFor(() => expect(screen.getByText(t('clips_refused'))).toBeDefined());
    expect(screen.getByText(t('clips_refused_hint'))).toBeDefined();
    // Nothing is hidden: the operator can still read what the core said.
    expect(screen.getByText("role 'dashboard' may not call 'clip.list'")).toBeDefined();
    // …but it is not the headline, and it is not an error banner.
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says the feature is not installed when the core does not know the request', async () => {
    await act(async () => {
      render(
        <ClipsPage
          t={t}
          lang="de"
          state={INITIAL_STATE}
          client={refusing(new IpcError('not_found', "unknown request 'clip.list'"))}
          onState={() => undefined}
        />,
      );
    });
    await waitFor(() => expect(screen.getByText(t('clips_unavailable'))).toBeDefined());
  });
});

describe('the Remote tab when remote access is off', () => {
  it('shows the off state and disables the pairing form instead of erroring twice', async () => {
    await act(async () => {
      render(
        <RemotePage
          t={t}
          lang="de"
          client={refusing(new IpcError('not_found', "unknown request 'remote.devices.list'"))}
          onOpenSettings={() => undefined}
        />,
      );
    });
    await waitFor(() => expect(screen.getByText(t('remote_disabled'))).toBeDefined());
    expect(screen.getByText(t('remote_unavailable'))).toBeDefined();
    // The form is gone rather than enabled-but-broken, and there is a way to switch it on.
    expect(screen.queryByRole('button', { name: t('remote_pair_button') })).toBeNull();
    expect(screen.getByRole('button', { name: t('remote_settings_link') })).toBeDefined();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('offers pairing once the core reports the feature enabled', async () => {
    const client = {
      request: (name: string) =>
        name === 'remote.devices.list'
          ? Promise.resolve({ devices: [], enabled: true })
          : Promise.resolve({}),
    } as unknown as IpcClient;
    await act(async () => {
      render(<RemotePage t={t} lang="de" client={client} onOpenSettings={() => undefined} />);
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: t('remote_pair_button') })).toBeDefined(),
    );
  });
});
