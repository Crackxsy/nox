/**
 * The Status page's ways back from safety: resuming from safe mode, the kill button after a
 * resume, and moving to a less private mode - each with the confirmation and the PIN the core asks
 * for, and each saying honestly what happened.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';
import { INITIAL_STATE, type DashboardState } from '../model';
import { StatusPage } from '../pages/Status';

const t = translator('de');

type Answer = Record<string, unknown> | IpcError;

interface Fake {
  client: IpcClient;
  sent: { name: string; payload: Record<string, unknown> }[];
}

/** Answers by request name; a list is consumed one answer per call. */
function fakeClient(answers: Record<string, Answer | Answer[]> = {}): Fake {
  const sent: Fake['sent'] = [];
  const client = {
    request: (name: string, payload: Record<string, unknown>) => {
      sent.push({ name, payload });
      const entry = answers[name];
      const answer = Array.isArray(entry) ? (entry.shift() ?? { ok: true }) : (entry ?? { ok: true });
      return answer instanceof IpcError ? Promise.reject(answer) : Promise.resolve(answer);
    },
  } as unknown as IpcClient;
  return { client, sent };
}

const PIN_SET = {
  configured: true,
  state: 'valid',
  min_length: 6,
  gate_required: true,
  resume_requires_pin: false,
  locked_until: null,
};

function page(fake: Fake, state: Partial<DashboardState>) {
  return (
    <StatusPage
      t={t}
      lang="de"
      state={{ ...INITIAL_STATE, connected: true, ...state }}
      client={fake.client}
      providersFailed={false}
      onRefresh={() => undefined}
    />
  );
}

const requests = (fake: Fake, name: string) => fake.sent.filter((s) => s.name === name);

/** "Übernehmen" exists twice on the page (privacy and assistant mode); look inside the tile. */
const privacyTile = () => within(document.getElementById('privacy') as HTMLElement);

describe('leaving safe mode', () => {
  it('offers Fortsetzen only while safe mode is on', () => {
    const fake = fakeClient();
    const { rerender } = render(page(fake, { systemLevel: 'running' }));
    expect(screen.queryByRole('button', { name: t('resume_button') })).toBeNull();
    rerender(page(fake, { systemLevel: 'safe_mode' }));
    expect(screen.getByRole('button', { name: t('resume_button') })).toBeDefined();
  });

  it('resumes a user kill without a PIN and says it is running again', async () => {
    const fake = fakeClient({ 'security.resume': { ok: true, supervisor: 'rearmed' } });
    const { rerender } = render(page(fake, { systemLevel: 'safe_mode' }));
    expect(screen.queryByLabelText(t('resume_pin_label'))).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('resume_button') }));
    });
    expect(requests(fake, 'security.resume')).toEqual([{ name: 'security.resume', payload: {} }]);

    rerender(page(fake, { systemLevel: 'running' }));
    expect(screen.getByText(t('resume_done'))).toBeDefined();
  });

  it('asks for the PIN after a security-path kill and sends it once', async () => {
    const fake = fakeClient({
      'security.pin.status': { ...PIN_SET, resume_requires_pin: true },
      'security.resume': { ok: true, supervisor: 'rearmed' },
    });
    render(page(fake, { systemLevel: 'safe_mode' }));
    const field = await screen.findByLabelText(t('resume_pin_label'));
    const button = screen.getByRole('button', { name: t('resume_button') });
    expect(button.hasAttribute('disabled')).toBe(true); // nothing to send yet

    fireEvent.change(field, { target: { value: '471108' } });
    await act(async () => {
      fireEvent.click(button);
    });

    expect(requests(fake, 'security.resume')).toEqual([
      { name: 'security.resume', payload: { pin: '471108' } },
    ]);
    expect((field as HTMLInputElement).value).toBe(''); // the PIN is not kept
  });

  it('says how many attempts are left after a wrong PIN, and when a lockout ends', async () => {
    const fake = fakeClient({
      'security.pin.status': { ...PIN_SET, resume_requires_pin: true },
      'security.resume': [
        { ok: false, reason: 'pin_wrong', remaining_attempts: 2 },
        { ok: false, reason: 'locked', locked_until: '2026-09-28T12:15:00Z' },
      ],
    });
    render(page(fake, { systemLevel: 'safe_mode' }));
    const field = await screen.findByLabelText(t('resume_pin_label'));

    fireEvent.change(field, { target: { value: '000000' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('resume_button') }));
    });
    expect(screen.getByRole('alert').textContent).toContain('Noch 2 Versuche');

    fireEvent.change(field, { target: { value: '000000' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('resume_button') }));
    });
    expect(screen.getByRole('alert').textContent).toContain('gesperrt bis');
  });

  it('shows the PIN field when the core asks for one the status did not mention', async () => {
    const fake = fakeClient({ 'security.resume': { ok: false, reason: 'pin_required' } });
    render(page(fake, { systemLevel: 'safe_mode' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('resume_button') }));
    });
    expect(screen.getByLabelText(t('resume_pin_label'))).toBeDefined();
    expect(screen.getByText(t('pin_refusal_required'))).toBeDefined();
  });

  it('says so when the watchdog could not be told', async () => {
    const fake = fakeClient({ 'security.resume': { ok: true, supervisor: 'unreachable' } });
    const { rerender } = render(page(fake, { systemLevel: 'safe_mode' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('resume_button') }));
    });
    rerender(page(fake, { systemLevel: 'running' }));
    expect(screen.getByText(t('resume_watchdog_unreachable'))).toBeDefined();
  });
});

describe('the kill switch after a resume', () => {
  it('is usable again once the core has left safe mode', async () => {
    const fake = fakeClient();
    const { rerender } = render(page(fake, { systemLevel: 'running' }));
    fireEvent.click(screen.getByRole('button', { name: t('kill_button') }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('kill_confirm_button') }));
    });
    expect(screen.getByRole('button', { name: t('kill_button') }).hasAttribute('disabled')).toBe(true);

    rerender(page(fake, { systemLevel: 'safe_mode' }));
    rerender(page(fake, { systemLevel: 'running' }));

    const button = screen.getByRole('button', { name: t('kill_button') });
    expect(button.hasAttribute('disabled')).toBe(false);
    fireEvent.click(button);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('kill_confirm_button') }));
    });
    expect(requests(fake, 'security.kill')).toHaveLength(2);
  });

  it('is not re-armed before the kill ever took effect', async () => {
    const fake = fakeClient();
    const { rerender } = render(page(fake, { systemLevel: 'starting' }));
    fireEvent.click(screen.getByRole('button', { name: t('kill_button') }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('kill_confirm_button') }));
    });
    rerender(page(fake, { systemLevel: 'running' })); // never went through safe mode
    expect(screen.getByRole('button', { name: t('kill_button') }).hasAttribute('disabled')).toBe(true);
  });
});

describe('moving to a less private mode', () => {
  it('asks before "Alles erlaubt" and sends the confirmation only after it', async () => {
    const fake = fakeClient({ 'privacy.set': { mode: 'full', applied: true } });
    render(page(fake, { privacyMode: 'balanced' }));
    fireEvent.change(screen.getByLabelText(t('privacy_mode')), { target: { value: 'full' } });
    fireEvent.click(privacyTile().getByRole('button', { name: t('privacy_apply') }));

    expect(requests(fake, 'privacy.set')).toHaveLength(0);
    expect(screen.getByText(t('privacy_full_confirm_title'))).toBeDefined();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('privacy_full_confirm_button') }));
    });
    expect(requests(fake, 'privacy.set')).toEqual([
      { name: 'privacy.set', payload: { mode: 'full', confirmed: true } },
    ]);
    expect(screen.getByText(t('privacy_applied'))).toBeDefined();
  });

  it('cancelling the confirmation sends nothing', () => {
    const fake = fakeClient();
    render(page(fake, { privacyMode: 'balanced' }));
    fireEvent.change(screen.getByLabelText(t('privacy_mode')), { target: { value: 'full' } });
    fireEvent.click(privacyTile().getByRole('button', { name: t('privacy_apply') }));
    fireEvent.click(screen.getByRole('button', { name: t('privacy_cancel') }));
    expect(requests(fake, 'privacy.set')).toHaveLength(0);
  });

  it('says "not applied" when the core did not apply it, instead of claiming success', async () => {
    const fake = fakeClient({
      'privacy.set': { mode: 'balanced', applied: false, requires_confirmation: true },
    });
    render(page(fake, { privacyMode: 'balanced' }));
    fireEvent.change(screen.getByLabelText(t('privacy_mode')), { target: { value: 'full' } });
    fireEvent.click(privacyTile().getByRole('button', { name: t('privacy_apply') }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('privacy_full_confirm_button') }));
    });
    expect(screen.getByRole('alert').textContent).toBe(t('privacy_not_applied'));
  });

  it('asks for the PIN for a relaxing change while a PIN is set, and names a wrong one', async () => {
    const fake = fakeClient({
      'security.pin.status': PIN_SET,
      'privacy.set': new IpcError('permission.denied', 'privacy.set denied: wrong PIN', false, {
        reason: 'pin_wrong',
        remaining_attempts: 4,
      }),
    });
    render(page(fake, { privacyMode: 'private' }));
    fireEvent.change(screen.getByLabelText(t('privacy_mode')), { target: { value: 'balanced' } });
    const field = await screen.findByLabelText(t('privacy_pin_label'));
    expect(privacyTile().getByRole('button', { name: t('privacy_apply') }).hasAttribute('disabled')).toBe(true);

    fireEvent.change(field, { target: { value: '000000' } });
    await act(async () => {
      fireEvent.click(privacyTile().getByRole('button', { name: t('privacy_apply') }));
    });

    expect(requests(fake, 'privacy.set')[0].payload).toEqual({ mode: 'balanced', pin: '000000' });
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Noch 4 Versuche'));
  });

  it('never asks for a PIN to become more private', async () => {
    const fake = fakeClient({ 'security.pin.status': PIN_SET, 'privacy.set': { mode: 'offline' } });
    render(page(fake, { privacyMode: 'balanced' }));
    await waitFor(() => expect(requests(fake, 'security.pin.status')).toHaveLength(1));
    fireEvent.change(screen.getByLabelText(t('privacy_mode')), { target: { value: 'offline' } });
    expect(screen.queryByLabelText(t('privacy_pin_label'))).toBeNull();
    await act(async () => {
      fireEvent.click(privacyTile().getByRole('button', { name: t('privacy_apply') }));
    });
    expect(requests(fake, 'privacy.set')[0].payload).toEqual({ mode: 'offline' });
  });
});
