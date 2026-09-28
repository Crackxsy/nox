/**
 * Settings → Sicherheits-PIN: the only place in the dashboard where a PIN is set, changed or
 * removed. Pinned here: the right fields for each state, nothing sent until the two new entries
 * agree and are long enough, removal asks twice, and every refusal is a sentence.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';
import type { PinState } from '../model';
import { PinSection } from '../pages/settings/PinSection';
import type { PinStatusView } from '../pages/usePinStatus';

const t = translator('de');

function pinState(overrides: Partial<PinState> = {}): PinState {
  return {
    state: 'not_set',
    configured: false,
    minLength: 6,
    gateRequired: false,
    resumeRequiresPin: false,
    lockedUntil: null,
    ...overrides,
  };
}

function view(pin: PinState | null, failed = false): PinStatusView {
  return { pin, failed, refresh: vi.fn(() => Promise.resolve()) };
}

function setup(status: PinStatusView, answer: Record<string, unknown> | IpcError = { ok: true }) {
  const sent: { name: string; payload: Record<string, unknown> }[] = [];
  const client = {
    request: (name: string, payload: Record<string, unknown>) => {
      sent.push({ name, payload });
      return answer instanceof IpcError ? Promise.reject(answer) : Promise.resolve(answer);
    },
  } as unknown as IpcClient;
  const onChanged = vi.fn();
  render(<PinSection t={t} lang="de" client={client} status={status} onChanged={onChanged} />);
  return { sent, onChanged };
}

const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe('setting the first PIN', () => {
  it('needs no current PIN and sends the new one once', async () => {
    const status = view(pinState());
    const { sent, onChanged } = setup(status);
    expect(screen.getByText(t('pin_state_not_set'))).toBeDefined();
    expect(screen.queryByLabelText(t('pin_current_label'))).toBeNull();

    type(t('pin_new_label'), '471108');
    type(t('pin_confirm_label'), '471108');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('pin_set_button') }));
    });

    expect(sent).toEqual([{ name: 'security.pin.set', payload: { pin: '471108' } }]);
    expect(screen.getByText(t('pin_saved'))).toBeDefined();
    expect(status.refresh).toHaveBeenCalledOnce();
    expect(onChanged).toHaveBeenCalledOnce();
    expect((screen.getByLabelText(t('pin_new_label')) as HTMLInputElement).value).toBe('');
  });

  it('sends nothing when the two entries differ', () => {
    const { sent } = setup(view(pinState()));
    type(t('pin_new_label'), '471108');
    type(t('pin_confirm_label'), '471109');
    fireEvent.click(screen.getByRole('button', { name: t('pin_set_button') }));
    expect(sent).toEqual([]);
    expect(screen.getByRole('alert').textContent).toBe(t('pin_mismatch'));
  });

  it('sends nothing for a PIN below the minimum and says the minimum', () => {
    const { sent } = setup(view(pinState()));
    type(t('pin_new_label'), '4711');
    type(t('pin_confirm_label'), '4711');
    fireEvent.click(screen.getByRole('button', { name: t('pin_set_button') }));
    expect(sent).toEqual([]);
    expect(screen.getByRole('alert').textContent).toContain('mindestens 6 Zeichen');
  });
});

describe('changing and removing a PIN', () => {
  const SET = pinState({ state: 'valid', configured: true });

  it('needs the current PIN to change it', async () => {
    const { sent } = setup(view(SET));
    const button = screen.getByRole('button', { name: t('pin_change_button') });
    type(t('pin_new_label'), '902211');
    type(t('pin_confirm_label'), '902211');
    expect(button.hasAttribute('disabled')).toBe(true);

    type(t('pin_current_label'), '471108');
    await act(async () => {
      fireEvent.click(button);
    });
    expect(sent).toEqual([
      { name: 'security.pin.set', payload: { pin: '902211', current_pin: '471108' } },
    ]);
  });

  it('names a wrong current PIN and the attempts left', async () => {
    setup(
      view(SET),
      new IpcError('permission.denied', 'pin.set denied: wrong PIN', false, {
        reason: 'pin_wrong',
        remaining_attempts: 3,
      }),
    );
    type(t('pin_current_label'), '000000');
    type(t('pin_new_label'), '902211');
    type(t('pin_confirm_label'), '902211');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('pin_change_button') }));
    });
    expect(screen.getByRole('alert').textContent).toContain('Noch 3 Versuche');
  });

  it('asks twice before removing, and only with the current PIN', async () => {
    const { sent } = setup(view(SET));
    fireEvent.click(screen.getByRole('button', { name: t('pin_remove_button') }));
    expect(sent).toEqual([]);
    expect(screen.getByText(t('pin_remove_explain'))).toBeDefined();
    // One "current PIN" field on screen at a time.
    expect(screen.getAllByLabelText(t('pin_current_label'))).toHaveLength(1);

    const confirm = screen.getByRole('button', { name: t('pin_remove_confirm_button') });
    expect(confirm.hasAttribute('disabled')).toBe(true);
    type(t('pin_current_label'), '471108');
    await act(async () => {
      fireEvent.click(confirm);
    });
    expect(sent).toEqual([{ name: 'security.pin.clear', payload: { current_pin: '471108' } }]);
    expect(screen.getByText(t('pin_removed'))).toBeDefined();
  });

  it('cancelling the removal sends nothing', () => {
    const { sent } = setup(view(SET));
    fireEvent.click(screen.getByRole('button', { name: t('pin_remove_button') }));
    fireEvent.click(screen.getByRole('button', { name: t('privacy_cancel') }));
    expect(sent).toEqual([]);
    expect(screen.getByRole('button', { name: t('pin_change_button') })).toBeDefined();
  });

  it('says until when a lockout runs', () => {
    setup(view(pinState({ state: 'valid', configured: true, lockedUntil: '2026-09-28T12:15:00Z' })));
    expect(screen.getByText(/Gesperrt bis/)).toBeDefined();
  });
});

describe('states that offer no form', () => {
  it('a raw value in the credential store points to `nox pin set`', () => {
    setup(view(pinState({ state: 'invalid', configured: true })));
    expect(screen.getByText(t('pin_state_invalid'))).toBeDefined();
    expect(screen.queryByLabelText(t('pin_new_label'))).toBeNull();
  });

  it('an unreachable credential store is said, not hidden', () => {
    setup(view(pinState({ state: 'unavailable', configured: true })));
    expect(screen.getByText(t('pin_state_unavailable'))).toBeDefined();
    expect(screen.queryByLabelText(t('pin_new_label'))).toBeNull();
  });

  it('a failed status read offers a retry', () => {
    const status = view(null, true);
    setup(status);
    expect(screen.getByText(t('pin_state_failed'))).toBeDefined();
    fireEvent.click(screen.getByRole('button', { name: t('chat_history_retry') }));
    expect(status.refresh).toHaveBeenCalledOnce();
  });

  it('while loading shows the shape, not a form', () => {
    setup(view(null));
    expect(screen.queryByLabelText(t('pin_new_label'))).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });
});
