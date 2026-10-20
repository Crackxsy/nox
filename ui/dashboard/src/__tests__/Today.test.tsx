/**
 * Heute (Spec UX-Neubau §3, §9): the greeting, "Wartet auf dich" with its yes/no answers, the
 * empty state that says what appears here and how to fill it, and the live view in the stream
 * Modus. The first run is pinned here too: three skippable steps, nothing written before "Weiter".
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import FirstRun from '../areas/FirstRun';
import { FIRST_RUN_KEY, firstRunDone } from '../areas/firstRunFlag';
import { Today, greetingKey } from '../areas/Today';
import { translator } from '../i18n';
import type { IpcClient } from '../ipc';
import { type DashboardState, INITIAL_STATE } from '../model';
import type { HomeController } from '../pages/Home';
import type { AppData } from '../shell/useAppData';

const t = translator('de');

const home: HomeController = {
  blocked: null,
  loading: false,
  pending: {},
  failed: {},
  refresh: () => undefined,
  toggle: vi.fn(),
  activate: vi.fn(),
};

function data(overrides: Partial<AppData> = {}): AppData {
  return {
    unconfirmed: [],
    fresh: [],
    skillsReady: 0,
    presets: [],
    userName: 'Mila',
    configLoaded: true,
    reload: () => undefined,
    dropFact: vi.fn(),
    ...overrides,
  };
}

function fake(answers: Record<string, unknown> = {}) {
  const calls: { name: string; payload: unknown }[] = [];
  const client = {
    request: (name: string, payload: unknown) => {
      calls.push({ name, payload });
      return Promise.resolve(answers[name] ?? {});
    },
  } as unknown as IpcClient;
  return { client, calls };
}

async function renderToday(state: DashboardState, appData: AppData, client: IpcClient | null, navigate = vi.fn(), onAsk = vi.fn()) {
  await act(async () => {
    render(
      <Today
        t={t}
        lang="de"
        state={state}
        onState={() => undefined}
        client={client}
        device={false}
        prompts={[]}
        promptProps={{ pinSet: null, onAnswered: () => undefined }}
        home={home}
        data={appData}
        navigate={navigate}
        onAsk={onAsk}
        now={new Date(2026, 9, 20, 19, 30)}
      />,
    );
  });
  return { navigate, onAsk };
}

describe('Heute', () => {
  it('greets by the time of day and by name', async () => {
    expect(greetingKey(8)).toBe('ux_greet_morning');
    expect(greetingKey(13)).toBe('ux_greet_day');
    expect(greetingKey(21)).toBe('ux_greet_evening');
    await renderToday(INITIAL_STATE, data(), null);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Guten Abend, Mila.');
  });

  it('lists a fact that waits for a yes, and sends the answer', async () => {
    const drop = vi.fn();
    const { client, calls } = fake();
    const fact = { id: 7, type: 'user', text: 'Jonas hat am 2. November Geburtstag.', createdAt: '', learned: true, unconfirmed: true };
    await renderToday(INITIAL_STATE, data({ unconfirmed: [fact], dropFact: drop }), client);
    const waiting = screen.getByRole('region', { name: t('ux_waiting_title') });
    expect(within(waiting).getByText('„Jonas hat am 2. November Geburtstag.“')).toBeTruthy();
    await act(async () => within(waiting).getByRole('button', { name: t('ux_fact_keep') }).click());
    expect(calls.filter((c) => c.name.startsWith('memory.'))).toEqual([{ name: 'memory.profile.confirm', payload: { id: 7 } }]);
    expect(drop).toHaveBeenCalledWith(7);
  });

  it('turns a part of Nox that is down into a line that leads to the details', async () => {
    const state: DashboardState = {
      ...INITIAL_STATE,
      health: { generatedAt: null, components: [{ component: 'voice', status: 'unavailable', reason: '' }] },
    };
    const { navigate } = await renderToday(state, data(), null);
    expect(screen.getByText(/geht gerade nicht$/)).toBeTruthy();
    expect(screen.getByText(t('ux_health_one'), { exact: false })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: t('ux_trouble_open') }));
    expect(navigate).toHaveBeenCalledWith({ area: 'settings', page: 'system' });
  });

  it('when nothing is connected yet, says what appears here and offers sentences and connections', async () => {
    const { client } = fake({ 'day.today': { configured: false, events: [], reminders: [], later: [], calendars: [] } });
    const { navigate, onAsk } = await renderToday(INITIAL_STATE, data(), client);
    expect(await screen.findByText(t('ux_today_empty_title'))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: t('ux_today_example_2') }));
    expect(onAsk).toHaveBeenCalledWith(t('ux_today_example_2'));
    fireEvent.click(screen.getByRole('button', { name: t('ux_connect_home') }));
    expect(navigate).toHaveBeenCalledWith({ area: 'settings', page: 'home' });
  });

  it('becomes the live view in the stream Modus', async () => {
    await renderToday({ ...INITIAL_STATE, mode: 'stream' }, data(), null);
    expect(await screen.findByRole('button', { name: t('stream_privacy_button') })).toBeTruthy();
    expect(screen.queryByRole('region', { name: t('ux_flows_title') })).toBeNull();
  });
});

describe('the first run', () => {
  it('walks three skippable steps and writes only what was answered', async () => {
    window.localStorage.removeItem(FIRST_RUN_KEY);
    const { client, calls } = fake({
      'config.get': {
        values: { 'voice.tts.engine': 'piper' },
        schema: [{ path: 'voice.tts.engine', type: 'enum', options: ['piper', 'supertonic'], group: 'voice' }],
      },
    });
    const done = vi.fn();
    await act(async () => {
      render(<FirstRun t={t} client={client} onDone={done} />);
    });
    expect(screen.getByRole('dialog', { name: t('ux_first_name_title') })).toBeTruthy();
    fireEvent.change(screen.getByLabelText(t('ux_first_name_label')), { target: { value: 'Mila' } });
    await act(async () => screen.getByRole('button', { name: t('ux_first_next') }).click());
    expect(calls.find((c) => c.name === 'config.set')?.payload).toEqual({ values: { 'identity.user_display_name': 'Mila' } });
    // Privacy: Standard is preselected; skipping sends nothing.
    expect((screen.getByRole('radio', { name: /^Standard/ }) as HTMLInputElement).checked).toBe(true);
    await act(async () => screen.getByRole('button', { name: t('ux_first_skip') }).click());
    expect(calls.some((c) => c.name === 'privacy.set')).toBe(false);
    await waitFor(() => expect(screen.getByRole('radio', { name: /Supertonic/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('radio', { name: /Supertonic/i }));
    await act(async () => screen.getByRole('button', { name: t('ux_first_done') }).click());
    expect(calls.filter((c) => c.name === 'config.set')[1]?.payload).toEqual({ values: { 'voice.tts.engine': 'supertonic' } });
    expect(done).toHaveBeenCalled();
    expect(firstRunDone()).toBe(true);
  });
});
