/**
 * Einstellungen as a component: the eight pages, the secret/PIN flow, and the reload bug that only
 * a mounted page can show.
 *
 * What is pinned here:
 *  - a secret's value is write-only and a PIN is sent exactly when the core says one is configured;
 *  - the PIN prompt belongs to one secret at a time (three visible secrets used to share one input,
 *    so typing in one filled all three and announced the same error three times);
 *  - a reload triggered from elsewhere never overwrites the personality text somebody is editing —
 *    the previous check compared against the first render's empty string and therefore always did;
 *  - the overview says, per page, how it is set right now - in words;
 *  - every setting lands on exactly the page the spec names, and nothing the core offers is lost.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import { IpcError, type IpcClient } from '../ipc';
import SettingsArea from '../areas/SettingsArea';
import { INITIAL_STATE } from '../model';
import type { SettingsPageId } from '../shell/routes';

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
              {
                path: 'desktop.computer_use.enabled',
                type: 'bool',
                restart_required: false,
                group: 'privacy',
              },
              { path: 'some.future.setting', type: 'string', restart_required: true, group: 'future' },
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

function area(client: IpcClient, page: SettingsPageId | null, revision = 0) {
  return (
    <SettingsArea
      t={t}
      lang="de"
      state={{ ...INITIAL_STATE, settingsRevision: revision, privacyMode: 'balanced', mode: 'companion' }}
      client={client}
      device={false}
      page={page}
      navigate={() => undefined}
      theme="system"
      onTheme={() => undefined}
      providersFailed={false}
      onRefreshProviders={() => undefined}
    />
  );
}

async function renderSettings(options: FakeOptions = {}, page: SettingsPageId | null = 'stream') {
  const { client, sent } = fakeClient(options);
  const view = render(area(client, page));
  // Wait for the loaded page, not the shell: the configuration arrives after the first answer.
  await waitFor(() => expect(sent.some((s) => s.name === 'config.get')).toBe(true));
  await act(async () => undefined);
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

describe('the pages', () => {
  it('lists the eight pages, each with its state in words', async () => {
    await renderSettings({}, null);
    const links = screen.getAllByRole('link');
    expect(links).toHaveLength(8);
    expect(links[0]!.textContent).toContain(t('ux_set_privacy'));
    expect(links[0]!.textContent).toContain(t('privacy_balanced'));
    expect(links[4]!.textContent).toContain('Nox');
  });

  it('says where settings are kept without printing a path (System)', async () => {
    await renderSettings({}, 'system');
    expect(screen.getByText(t('settings_file'))).toBeDefined();
    expect(screen.queryByText(/user\.yaml/)).toBeNull();
  });

  it('keeps a setting no page names, under System › Mehr Optionen', async () => {
    await renderSettings({}, 'system');
    const field = await screen.findByLabelText('some.future.setting');
    expect(field.closest('details')).not.toBeNull();
  });

  it('explains computer control next to its switch (Privatsphäre)', async () => {
    await renderSettings({}, 'privacy');
    const box = await screen.findByLabelText(t('setting_desktop_computer_use_enabled'));
    expect(box.getAttribute('type')).toBe('checkbox');
    expect((box as HTMLInputElement).checked).toBe(false);
    const hint = document.getElementById(box.getAttribute('aria-describedby') ?? '');
    expect(hint?.textContent).toBe(t('setting_desktop_computer_use_enabled_hint'));
  });

  it('labels an enum option with words, not with its value (Stimme)', async () => {
    await renderSettings({}, 'voice');
    expect(await screen.findByRole('option', { name: 'Dauerhaft, nach Wake-Word' })).toBeDefined();
    expect(screen.queryByRole('option', { name: 'ptt_only' })).toBeNull();
  });

  it('does not overwrite a personality the user is editing when a reload arrives (Nox)', async () => {
    const { client, view } = await renderSettings({}, 'nox');
    const box = (await screen.findByLabelText(t('personality_label'))) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'Mein eigener Text' } });
    // A `settings.changed` elsewhere bumps the revision, which reloads everything.
    view.rerender(area(client, 'nox', 1));
    await act(async () => undefined);
    expect(box.value).toBe('Mein eigener Text');
  });
});
