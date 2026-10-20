/**
 * The shell around every page (Spec UX-Neubau §3-5): the four areas and their keys, old tab ids,
 * Stopp (at once, no question - the owner's decision) and Fortsetzen (the core's resume rules,
 * PIN included), the command palette from the keyboard, and what a paired device may not do.
 */

import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Auth } from '../auth';
import type { DashboardIpcHandlers } from '../ipc';

const harness = vi.hoisted(() => ({
  handlers: null as DashboardIpcHandlers | null,
  answers: {} as Record<string, unknown>,
  calls: [] as { name: string; payload: unknown }[],
}));

vi.mock('../ipc', async (importOriginal) => {
  const real = await importOriginal<typeof import('../ipc')>();
  return {
    ...real,
    createDashboardClient: vi.fn(async (_auth: Auth, handlers: DashboardIpcHandlers) => {
      harness.handlers = handlers;
      return {
        request: (name: string, payload: unknown) => {
          harness.calls.push({ name, payload });
          const answer = harness.answers[name];
          return typeof answer === 'function' ? (answer as (p: unknown) => Promise<unknown>)(payload) : Promise.resolve(answer ?? {});
        },
        close: () => undefined,
      };
    }),
  };
});

const { App } = await import('../App');
const { translator } = await import('../i18n');

const t = translator('de');
const LOCAL: Auth = { kind: 'local', token: 'tok' };
const DEVICE: Auth = { kind: 'device', token: 'x'.repeat(43), clientId: 'device:abc', deviceId: 'abc', name: 'Tablet' };

function event(name: string, payload: Record<string, unknown>) {
  return { v: 1, id: `e-${Math.random()}`, ts: new Date().toISOString(), kind: 'event' as const, name, corr: null, src: { role: 'core' as const, id: 'core' }, payload };
}

async function start(auth: Auth = LOCAL, hash = '#/today') {
  window.history.replaceState(null, '', hash);
  await act(async () => {
    render(<App auth={auth} lang="de" theme="system" />);
  });
  await act(async () => {
    harness.handlers!.onStatus('online');
  });
}

async function emit(name: string, payload: Record<string, unknown>) {
  await act(async () => {
    harness.handlers!.onEvent(event(name, payload));
  });
}

const called = (name: string) => harness.calls.filter((c) => c.name === name);
const stopButton = () => screen.queryByRole('button', { name: t('ux_stop') });

beforeEach(() => {
  harness.handlers = null;
  harness.calls = [];
  harness.answers = { 'state.get': { system: { level: 'running' }, assistant: { mode: 'companion' } } };
});

describe('the four areas', () => {
  it('starts on Heute and reaches each area from the side bar, focus on the page afterwards', async () => {
    await start();
    expect(document.querySelector('.app')!.getAttribute('data-area')).toBe('today');
    const nav = screen.getAllByRole('navigation', { name: t('nav') })[0]!;
    await act(async () => within(nav).getByRole('link', { name: t('ux_area_settings') }).click());
    expect(document.querySelector('.app')!.getAttribute('data-area')).toBe('settings');
    expect(window.location.hash).toBe('#/settings');
    expect(await screen.findByRole('heading', { level: 1, name: t('ux_area_settings') })).toBeTruthy();
    await act(async () => new Promise((r) => setTimeout(r, 5)));
    expect(document.activeElement?.id).toBe('main');
  });

  it('answers Alt+1..4 outside text fields only', async () => {
    await start();
    await act(async () => {
      fireEvent.keyDown(window, { key: '2', altKey: true });
    });
    expect(document.querySelector('.app')!.getAttribute('data-area')).toBe('nox');
    const input = await screen.findByLabelText(t('chat_input_label'));
    await act(async () => {
      fireEvent.keyDown(input, { key: '4', altKey: true });
    });
    expect(document.querySelector('.app')!.getAttribute('data-area')).toBe('nox');
  });

  it('sends the old tab ids where their content lives now', async () => {
    await start(LOCAL, '#/audit');
    expect(document.querySelector('.app')!.getAttribute('data-area')).toBe('nox');
    expect(await screen.findByRole('tab', { name: t('ux_tab_history'), selected: true })).toBeTruthy();
  });
});

describe('Stopp', () => {
  it('stops at once - one security.kill, no question first', async () => {
    harness.answers['security.kill'] = { ok: true };
    await start();
    await act(async () => stopButton()!.click());
    expect(called('security.kill')).toHaveLength(1);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText(t('ux_stopped_title'))).toBeTruthy();
    // No second Stopp while stopped, and the input says why it is locked.
    expect(stopButton()).toBeNull();
    expect((screen.getByLabelText(t('ux_composer_placeholder')) as HTMLInputElement).disabled).toBe(true);
  });

  it('is on Ctrl+Alt+Pause too', async () => {
    harness.answers['security.kill'] = { ok: true };
    await start();
    await act(async () => {
      fireEvent.keyDown(window, { key: 'Pause', ctrlKey: true, altKey: true });
    });
    expect(called('security.kill')).toHaveLength(1);
  });

  it('shows the stop the core reports, with its time, even when another device pressed it', async () => {
    await start();
    await emit('security.kill_switch', { origin: 'device' });
    const strip = screen.getByText(t('ux_stopped_title')).closest('[role="alert"]')!;
    expect(strip.textContent).toMatch(/Seit \d\d:\d\d läuft nichts mehr/);
    expect(stopButton()).toBeNull();
  });

  it('resumes with Fortsetzen and gives Stopp back', async () => {
    harness.answers['security.resume'] = { ok: true };
    await start();
    await emit('security.kill_switch', { origin: 'ui' });
    await act(async () => screen.getByRole('button', { name: t('ux_resume') }).click());
    expect(called('security.resume')).toEqual([{ name: 'security.resume', payload: {} }]);
    expect(screen.queryByText(t('ux_stopped_title'))).toBeNull();
    expect(stopButton()).not.toBeNull();
  });

  it('asks for the PIN when the core wants one, and sends it with the second try', async () => {
    let tries = 0;
    harness.answers['security.resume'] = () => Promise.resolve({ ok: ++tries > 1 });
    await start();
    await emit('security.kill_switch', { origin: 'ui' });
    await act(async () => screen.getByRole('button', { name: t('ux_resume') }).click());
    expect(screen.getByText(t('ux_resume_pin'))).toBeTruthy();
    fireEvent.change(screen.getByLabelText(t('perm_pin')), { target: { value: '2468' } });
    await act(async () => screen.getByRole('button', { name: t('ux_resume') }).click());
    expect(called('security.resume')[1]?.payload).toEqual({ pin: '2468' });
    expect(screen.queryByText(t('ux_stopped_title'))).toBeNull();
  });

  it('counts only an explicit ok as resumed', async () => {
    harness.answers['security.resume'] = {};
    await start();
    await emit('security.kill_switch', { origin: 'ui' });
    await act(async () => screen.getByRole('button', { name: t('ux_resume') }).click());
    expect(screen.getByText(t('ux_stopped_title'))).toBeTruthy();
    expect(screen.getByText(t('ux_resume_unclear'))).toBeTruthy();
    expect(stopButton()).toBeNull();
  });

  it('on a paired device: stopping works, resuming is not offered', async () => {
    harness.answers['security.kill'] = { ok: true };
    await start(DEVICE);
    await act(async () => stopButton()!.click());
    expect(called('security.kill')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: t('ux_resume') })).toBeNull();
    expect(screen.getByText(t('ux_stopped_device'), { exact: false })).toBeTruthy();
  });
});

describe('the command palette', () => {
  it('opens on Ctrl+K, moves with the arrows, runs with Enter and closes with Escape', async () => {
    await start();
    await act(async () => {
      fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    });
    const dialog = await screen.findByRole('dialog', { name: t('ux_palette') });
    const combo = within(dialog).getByRole('combobox');
    expect(document.activeElement).toBe(combo);
    fireEvent.change(combo, { target: { value: 'verlauf' } });
    const option = within(dialog).getByRole('option', { name: new RegExp(t('ux_tab_history')) });
    expect(combo.getAttribute('aria-activedescendant')).toBe(option.id);
    await act(async () => {
      fireEvent.keyDown(combo, { key: 'Enter' });
    });
    expect(document.querySelector('.app')!.getAttribute('data-area')).toBe('nox');
    expect(window.location.hash).toBe('#/nox/history');

    await act(async () => {
      fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    });
    const again = await screen.findByRole('dialog', { name: t('ux_palette') });
    const field = within(again).getByRole('combobox');
    fireEvent.change(field, { target: { value: 'Was steht heute an?' } });
    const options = within(again).getAllByRole('option');
    // "Nox fragen" is always last.
    expect(options[options.length - 1]!.textContent).toContain('Nox fragen');
    fireEvent.keyDown(field, { key: 'ArrowUp' });
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    expect(again.hasAttribute('open')).toBe(false);
  });

  it('sends "Nox fragen" to the conversation', async () => {
    harness.answers['chat.send'] = { request_id: 'r', text: 'Sonnig.', provider: 'ai.rules', degraded: false };
    await start();
    await act(async () => {
      fireEvent.change(screen.getByLabelText(t('ux_composer_placeholder')), { target: { value: 'W' } });
    });
    const combo = within(await screen.findByRole('dialog', { name: t('ux_palette') })).getByRole('combobox');
    fireEvent.change(combo, { target: { value: 'Wie wird das Wetter?' } });
    await act(async () => {
      fireEvent.keyDown(combo, { key: 'End' });
      fireEvent.keyDown(combo, { key: 'Enter' });
    });
    expect(window.location.hash).toBe('#/nox/talk');
    await screen.findByText('Sonnig.');
    expect(called('chat.send')[0]?.payload).toEqual({ text: 'Wie wird das Wetter?' });
  });

  it('runs a sentence through the home command layer and says how fast it matched', async () => {
    harness.answers['home.status'] = { connected: true, host: 'ha', port: 8123, token_present: true };
    harness.answers['home.list'] = { ok: true, connected: true, areas: ['Wohnzimmer'], areas_available: true, entities: [] };
    harness.answers['home.command'] = { matched: true, refused: false, reason: '', tool: 'home.light', summary: 'Licht Wohnzimmer: aus', match_ms: 0.4 };
    await start();
    await act(async () => {
      fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    });
    const dialog = await screen.findByRole('dialog', { name: t('ux_palette') });
    const combo = within(dialog).getByRole('combobox');
    fireEvent.change(combo, { target: { value: 'mach das licht aus' } });
    await act(async () => within(dialog).getByRole('option', { name: /Als Befehl ausführen/ }).click());
    expect(within(dialog).getByRole('status').textContent).toMatch(/Licht Wohnzimmer: aus .*0,4 ms/);
  });
});

describe('the header', () => {
  it('counts what waits and takes the person there', async () => {
    harness.answers['memory.profile.list'] = { items: [{ id: 3, text: 'Mag Tee.', created_at: new Date().toISOString(), learned: true, unconfirmed: true }] };
    await start(LOCAL, '#/settings');
    const chip = await screen.findByRole('button', { name: '1 Sache wartet auf dich' });
    await act(async () => chip.click());
    expect(document.querySelector('.app')!.getAttribute('data-area')).toBe('today');
    expect(screen.getByText('„Mag Tee.“')).toBeTruthy();
  });

  it('opens one Modus menu; switching sends mode.set (mode and profile together) only on the button', async () => {
    harness.answers['mode.set'] = { ok: true, mode: 'stream', profile: 'stream' };
    await start();
    await act(async () => screen.getByRole('button', { name: new RegExp(t('ux_mode_open')) }).click());
    const menu = await screen.findByRole('dialog', { name: t('mode_title') });
    const stream = await within(menu).findByRole('radio', { name: /^Stream/ });
    await act(async () => fireEvent.click(stream));
    expect(called('mode.set')).toHaveLength(0);
    await act(async () => within(menu).getByRole('button', { name: 'Zu „Stream“ wechseln' }).click());
    expect(called('mode.set')[0]?.payload).toEqual({ mode: 'stream' });
    expect(within(menu).getByText(/Jetzt: Stream-Modus\. Regeln: /)).toBeTruthy();
  });

  it('keeps the Modus read-only on a paired device, mute stays allowed', async () => {
    harness.answers['state.get'] = { assistant: { mode: 'companion', muted: false }, system: { level: 'running' } };
    await start(DEVICE);
    await act(async () => screen.getByRole('button', { name: new RegExp(t('ux_mode_open')) }).click());
    const menu = await screen.findByRole('dialog', { name: t('mode_title') });
    const radios = await within(menu).findAllByRole('radio');
    expect(radios.every((r) => (r as HTMLInputElement).disabled)).toBe(true);
    expect(within(menu).getByText(t('ux_mode_device'))).toBeTruthy();
    expect((within(menu).getByRole('switch', { name: t('ux_mute') }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('the privacy chip', () => {
  it('carries a short word for the phone and the full name in its label', async () => {
    harness.answers['state.get'] = { privacy: { mode: 'private' }, system: { level: 'running' } };
    await start();
    const chip = screen.getByRole('button', { name: /^Privatsphäre: Nur lokal/ });
    expect(chip.querySelector('.chip-short')!.textContent).toBe(t('ux_privacy_short_private'));
  });
});

describe('a paired device', () => {
  it('sees Einstellungen read-only, with one line saying where it works', async () => {
    await start(DEVICE, '#/settings/voice');
    expect(await screen.findByText(t('device_admin_note'))).toBeTruthy();
    const fieldset = document.querySelector('fieldset.fieldset-plain') as HTMLFieldSetElement;
    expect(fieldset.disabled).toBe(true);
  });

  it('sees the skills read-only too', async () => {
    await start(DEVICE, '#/devices/skills');
    expect(await screen.findByText(t('device_admin_note'))).toBeTruthy();
  });
});

describe('review fixes', () => {
  const question = {
    request_id: 'q1',
    agent: 'companion',
    tool: 'file',
    action: 'delete',
    mode: 'companion',
    risk: 'medium',
    target: 'a.txt',
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    remember_ttl_s: 0,
  };

  it('closes the Modus menu when a question comes, and gives focus back after the answer', async () => {
    harness.answers['security.permission.reply'] = { ok: true };
    await start(LOCAL, '#/settings');
    const open = screen.getByRole('button', { name: new RegExp(t('ux_mode_open')) });
    await act(async () => open.click());
    const menu = await screen.findByRole('dialog', { name: t('mode_title') });
    await emit('security.permission_requested', question);
    expect(menu.hasAttribute('open')).toBe(false);
    const sheet = await screen.findByRole('alertdialog');
    await act(async () => within(sheet).getByRole('button', { name: t('perm_deny') }).click());
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(called('security.permission.reply')[0]?.payload).toMatchObject({ decision: 'deny' });
  });

  it('keeps focus on the tab while the arrow keys move between tabs', async () => {
    await start(LOCAL, '#/nox/talk');
    const tab = await screen.findByRole('tab', { name: t('ux_tab_talk') });
    tab.focus();
    await act(async () => {
      fireEvent.keyDown(tab, { key: 'ArrowRight' });
    });
    await act(async () => new Promise((r) => setTimeout(r, 5)));
    expect(document.activeElement?.getAttribute('role')).toBe('tab');
    expect(document.activeElement?.getAttribute('aria-selected')).toBe('true');
  });

  it('moves focus to the stopped strip when the pressed Stopp button goes away', async () => {
    harness.answers['security.kill'] = { ok: true };
    await start();
    const button = stopButton()!;
    button.focus();
    await act(async () => button.click());
    await act(async () => new Promise((r) => setTimeout(r, 5)));
    expect(document.activeElement?.id).toBe('stopped-strip');
  });
});

describe('Stopp is always reachable', () => {
  const question = {
    request_id: 'q9',
    agent: 'companion',
    tool: 'file',
    action: 'delete',
    mode: 'companion',
    risk: 'high',
    target: 'b.txt',
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    remember_ttl_s: 0,
  };

  beforeEach(() => {
    harness.answers['security.kill'] = { ok: true };
  });

  it('inside the permission sheet, while the page behind it is inert', async () => {
    await start(LOCAL, '#/settings');
    await emit('security.permission_requested', question);
    const sheet = await screen.findByRole('alertdialog');
    await act(async () => within(sheet).getByRole('button', { name: t('ux_stop') }).click());
    expect(called('security.kill')).toHaveLength(1);
  });

  it('inside the Modus and privacy menus', async () => {
    await start();
    await act(async () => screen.getByRole('button', { name: new RegExp(t('ux_mode_open')) }).click());
    const menu = await screen.findByRole('dialog', { name: t('mode_title') });
    await act(async () => within(menu).getByRole('button', { name: t('ux_stop') }).click());
    expect(called('security.kill')).toHaveLength(1);
    // Stopped: no Stopp is offered anywhere any more.
    expect(within(menu).queryByRole('button', { name: t('ux_stop') })).toBeNull();
  });

  it('inside the command line', async () => {
    await start();
    await act(async () => {
      fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    });
    const palette = await screen.findByRole('dialog', { name: t('ux_palette') });
    await act(async () => within(palette).getByRole('button', { name: t('ux_stop') }).click());
    expect(called('security.kill')).toHaveLength(1);
  });

  it('during the first run', async () => {
    window.localStorage.removeItem('nox.firstRun');
    harness.answers['config.get'] = { values: { 'identity.user_display_name': '' }, schema: [] };
    await start();
    const first = await screen.findByRole('dialog', { name: t('ux_first_name_title') });
    await act(async () => within(first).getByRole('button', { name: t('ux_stop') }).click());
    expect(called('security.kill')).toHaveLength(1);
    window.localStorage.setItem('nox.firstRun', 'done');
  });
});
