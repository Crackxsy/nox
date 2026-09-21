/**
 * The Settings page as a component: the secret/PIN flow, and the two reload bugs that only a
 * mounted page can show.
 *
 * What is pinned here:
 *  - a secret's value is write-only and a PIN is sent exactly when the core says one is configured;
 *  - the PIN prompt belongs to one secret at a time (three visible secrets used to share one input,
 *    so typing in one filled all three and announced the same error three times);
 *  - a reload triggered from elsewhere never overwrites the personality text somebody is editing —
 *    the previous check compared against the first render's empty string and therefore always did;
 *  - credentials come first on the page, because that is what a first-time user has to reach.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import { IpcError, type IpcClient } from '../ipc';
import { SettingsPage } from '../pages/settings/SettingsPage';
import { INITIAL_PLUGIN_STATUS } from '../model';

const t = translator('de');

interface Sent {
  name: string;
  payload: Record<string, unknown>;
}

interface FakeOptions {
  pinConfigured?: boolean;
  secretPresent?: string[];
  personality?: string;
  failSecretSet?: IpcError;
}

function fakeClient(options: FakeOptions = {}) {
  const sent: Sent[] = [];
  const present = new Set(options.secretPresent ?? []);
  const client = {
    request: (name: string, payload: Record<string, unknown> = {}) => {
      sent.push({ name, payload });
      switch (name) {
        case 'config.get':
          return Promise.resolve({
            values: { 'identity.name': 'Nox', 'voice.stt.listening_mode': 'continuous' },
            schema: [
              { path: 'identity.name', type: 'string', restart_required: false, group: 'identity' },
              {
                path: 'voice.stt.listening_mode',
                type: 'enum',
                options: ['continuous', 'ptt_only'],
                restart_required: true,
                group: 'voice',
              },
            ],
            user_config_path: 'C:\\Users\\x\\user.yaml',
          });
        case 'secrets.status':
          return Promise.resolve({
            secrets: [
              { name: 'nox/twitch/client_id', present: present.has('nox/twitch/client_id'), group: 'twitch' },
              { name: 'nox/obs/websocket_password', present: present.has('nox/obs/websocket_password'), group: 'obs' },
              { name: 'nox/telegram/bot_token', present: present.has('nox/telegram/bot_token'), group: 'telegram' },
            ],
          });
        case 'personality.get':
          return Promise.resolve({ text: options.personality ?? 'Du bist Nox.', path: 'p.md' });
        case 'health.history':
          return Promise.resolve({ entries: [] });
        case 'security.pin.status':
          return Promise.resolve({ configured: options.pinConfigured === true });
        case 'twitch.auth.status':
          return Promise.resolve({ state: 'idle' });
        case 'secrets.set':
          if (options.failSecretSet) return Promise.reject(options.failSecretSet);
          present.add(String(payload.name));
          return Promise.resolve({ ok: true });
        default:
          return Promise.resolve({ ok: true });
      }
    },
  } as unknown as IpcClient;
  return { client, sent };
}

async function renderSettings(options: FakeOptions = {}, revision = 0) {
  const { client, sent } = fakeClient(options);
  const view = render(
    <SettingsPage
      t={t}
      lang="de"
      client={client}
      revision={revision}
      twitchAuthEvent={null}
      plugins={INITIAL_PLUGIN_STATUS}
      onOpenRemote={() => undefined}
    />,
  );
  // Wait for the *loaded* page, not the shell: the personality box and the credential tiles exist
  // before the first answer arrives, the config group headings do not.
  await screen.findByRole('heading', { level: 4, name: t('settings_group_identity') });
  return { sent, view, client };
}

describe('credentials', () => {
  it('sends no pin field at all when the core reports none is configured', async () => {
    const { sent } = await renderSettings();
    const field = screen.getByLabelText(t('obs_password_label'));
    fireEvent.change(field, { target: { value: 'hunter2' } });
    const save = screen.getAllByRole('button', { name: t('secret_save') })[1];
    await act(async () => {
      fireEvent.click(save);
    });
    const set = sent.find((s) => s.name === 'secrets.set');
    expect(set).toBeDefined();
    expect('pin' in (set?.payload ?? {})).toBe(false);
    expect(set?.payload.value).toBe('hunter2');
  });

  it('sends the PIN with the secret when one is configured', async () => {
    const { sent } = await renderSettings({ pinConfigured: true });
    fireEvent.change(screen.getByLabelText(t('obs_password_label')), {
      target: { value: 'hunter2' },
    });
    fireEvent.change(screen.getByLabelText(t('pin_label')), { target: { value: '1234' } });
    await act(async () => {
      fireEvent.click(screen.getAllByRole('button', { name: t('secret_save') })[1]);
    });
    const set = sent.find((s) => s.name === 'secrets.set');
    expect(set?.payload).toMatchObject({ name: 'nox/obs/websocket_password', pin: '1234' });
  });

  it('shows the PIN prompt for exactly one secret at a time', async () => {
    await renderSettings({ pinConfigured: true });
    // Three credentials are on screen and none of them owns the prompt yet.
    expect(screen.queryAllByLabelText(t('pin_label'))).toHaveLength(0);
    fireEvent.change(screen.getByLabelText(t('obs_password_label')), {
      target: { value: 'hunter2' },
    });
    // Filling one in gives that one — and only that one — the prompt.
    expect(screen.getAllByLabelText(t('pin_label'))).toHaveLength(1);
  });

  it('never renders a stored secret as a readable value', async () => {
    await renderSettings({ secretPresent: ['nox/obs/websocket_password'] });
    expect(screen.getAllByText(t('secret_stored')).length).toBeGreaterThan(0);
    expect(screen.queryByDisplayValue('hunter2')).toBeNull();
  });

  it('turns a PIN refusal into the honest message instead of a raw error line', async () => {
    await renderSettings({
      pinConfigured: true,
      failSecretSet: new IpcError('permission.denied', 'PIN required for secret changes'),
    });
    fireEvent.change(screen.getByLabelText(t('obs_password_label')), {
      target: { value: 'hunter2' },
    });
    await act(async () => {
      fireEvent.click(screen.getAllByRole('button', { name: t('secret_save') })[1]);
    });
    await waitFor(() => expect(screen.getByText(t('pin_required'))).toBeDefined());
    expect(screen.queryByText(/permission\.denied/)).toBeNull();
  });
});

describe('the page as a whole', () => {
  it('puts the credentials section before the configuration form', async () => {
    await renderSettings();
    const headings = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent);
    expect(headings.indexOf(t('integrations_title'))).toBeLessThan(
      headings.indexOf(t('settings_title')),
    );
  });

  it('says where settings are kept without printing a path under the headline', async () => {
    await renderSettings();
    expect(screen.getByText(t('settings_file'))).toBeDefined();
    expect(screen.queryByText(/user\.yaml/)).toBeNull();
  });

  it('labels an enum option with words, not with its value', async () => {
    await renderSettings();
    expect(screen.getByRole('option', { name: 'Dauerhaft, nach Wake-Word' })).toBeDefined();
    expect(screen.queryByRole('option', { name: 'ptt_only' })).toBeNull();
  });

  it('does not overwrite a personality the user is editing when a reload arrives', async () => {
    const { client } = await renderSettings();
    const box = screen.getByLabelText(t('personality_label')) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'Mein eigener Text' } });
    // A `settings.changed` elsewhere bumps the revision, which reloads everything.
    render(
      <SettingsPage
        t={t}
        lang="de"
        client={client}
        revision={1}
        twitchAuthEvent={null}
        plugins={INITIAL_PLUGIN_STATUS}
        onOpenRemote={() => undefined}
      />,
    );
    expect(box.value).toBe('Mein eigener Text');
  });
});
