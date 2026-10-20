/**
 * The Modus and privacy forms (Spec UX-Neubau §5, owner decision 1): one "Modus" for mode and
 * security profile, applied only on the button; "Alles erlaubt" only with an explicit yes; relaxing
 * privacy asks for the PIN when the core says so.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';
import { INITIAL_STATE } from '../model';
import { ModeChoice, PrivacyChoice, captureLine } from '../shell/choices';

const t = translator('de');
const state = { ...INITIAL_STATE, mode: 'companion', privacyMode: 'balanced' };

function client(answer: (name: string, payload: Record<string, unknown>) => Promise<unknown>) {
  const calls: { name: string; payload: Record<string, unknown> }[] = [];
  const c = {
    request: vi.fn((name: string, payload: Record<string, unknown>) => {
      calls.push({ name, payload });
      return answer(name, payload);
    }),
  } as unknown as IpcClient;
  return { c, calls };
}

describe('ModeChoice', () => {
  it('selects with the radio and applies only with the button, then names the rules in force', async () => {
    const { c, calls } = client(() => Promise.resolve({ ok: true, mode: 'coding', profile: 'coding' }));
    render(<ModeChoice t={t} state={state} client={c} device={false} />);
    fireEvent.click(screen.getByRole('radio', { name: /^Programmieren/ }));
    expect(calls).toHaveLength(0);
    await act(async () => screen.getByRole('button', { name: 'Zu „Programmieren“ wechseln' }).click());
    expect(calls).toEqual([{ name: 'mode.set', payload: { mode: 'coding' } }]);
    expect(screen.getByRole('status').textContent).toBe(`Jetzt: Programmieren-Modus. Regeln: ${t('profile_coding')}.`);
  });

  it('can be cancelled without sending anything', () => {
    const { c, calls } = client(() => Promise.resolve({}));
    render(<ModeChoice t={t} state={state} client={c} device={false} />);
    fireEvent.click(screen.getByRole('radio', { name: /^Fokus/ }));
    fireEvent.click(screen.getByRole('button', { name: t('kill_cancel') }));
    expect(calls).toHaveLength(0);
    expect((screen.getByRole('radio', { name: /^Begleiter/ }) as HTMLInputElement).checked).toBe(true);
  });

  it('shows the profile under "Technische Angaben", never as a second control', () => {
    render(<ModeChoice t={t} state={{ ...state, mode: 'stream' }} client={null} device={false} />);
    const details = screen.getByText(t('perm_technical')).closest('details')!;
    expect(details.textContent).toContain(t('profile_stream'));
    expect(screen.queryByRole('combobox')).toBeNull();
  });
});

describe('PrivacyChoice', () => {
  it('asks in words before "Alles erlaubt", and sends the explicit yes', async () => {
    const { c, calls } = client(() => Promise.resolve({ mode: 'full' }));
    render(<PrivacyChoice t={t} state={state} client={c} device={false} />);
    fireEvent.click(screen.getByRole('radio', { name: /^Alles erlaubt/ }));
    expect(screen.getByText(t('ux_privacy_full_confirm'))).toBeTruthy();
    await act(async () => screen.getByRole('button', { name: t('ux_privacy_full_yes') }).click());
    expect(calls[0]).toEqual({ name: 'privacy.set', payload: { mode: 'full', confirmed: true } });
  });

  it('asks for the PIN when the core wants it, and sends it with the second try', async () => {
    let tries = 0;
    const { c, calls } = client(() =>
      ++tries === 1 ? Promise.reject(new IpcError('permission.denied', 'PIN required')) : Promise.resolve({}),
    );
    render(<PrivacyChoice t={t} state={{ ...state, privacyMode: 'private' }} client={c} device={false} />);
    fireEvent.click(screen.getByRole('radio', { name: /^Standard/ }));
    await act(async () => screen.getByRole('button', { name: t('privacy_apply') }).click());
    expect(screen.getByRole('alert').textContent).toBe(t('pin_required'));
    fireEvent.change(screen.getByLabelText(t('perm_pin')), { target: { value: '1357' } });
    await act(async () => screen.getByRole('button', { name: t('privacy_apply') }).click());
    expect(calls[1]?.payload).toEqual({ mode: 'balanced', pin: '1357' });
  });

  it('is read-only on a paired device', () => {
    render(<PrivacyChoice t={t} state={state} client={{} as IpcClient} device />);
    expect(screen.getAllByRole('radio').every((r) => (r as HTMLInputElement).disabled)).toBe(true);
    expect(screen.getByText(t('ux_privacy_device'))).toBeTruthy();
  });

  it('says what is recording right now, in words', () => {
    expect(captureLine(t, { microphone: true, camera: false, screen: true, cloud: false })).toBe(
      `Gerade aktiv: ${t('capture_microphone')}, ${t('capture_screen')}.`,
    );
    expect(captureLine(t, null)).toBe(t('ux_capture_unknown'));
  });
});
