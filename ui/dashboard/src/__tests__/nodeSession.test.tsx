/**
 * Refusals while Nox controls mouse and keyboard on a paired PC (`node_session`): every one has
 * its own sentence, from the core's second word when it sends one and from its sentence when it
 * does not yet - never "Die PIN stimmt nicht", never "Die Antwort ist nicht angekommen", never the
 * core's English.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { refusalText } from '../hooks';
import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';
import { type PermissionPrompt, pinErrorKey } from '../model';
import { PermissionPromptDialog } from '../PermissionPrompt';

const t = translator('de');

// The core's sentences today (`nox.node.world_routes`), made up nowhere else.
const LINKED =
  'Nox controls mouse and keyboard on the PC this browser runs on; nothing can be approved or changed from here until that ends - deny or stop';
const REMOTE =
  'Nox controls mouse and keyboard on a paired PC and this computer is used remotely (or that cannot be told); approve at the computer itself with the PIN, or deny or stop';
const NO_PIN =
  'Nox controls mouse and keyboard on a paired PC; approving or changing anything now needs the security PIN, and none is set - deny, stop, or set a PIN first';
const SATURATED =
  'Nox steuert ein Gerät mit sehr vielen Adressen - Freigaben und Nachrichten erst nach Ende der Sitzung; Ablehnen und Stopp gehen immer';

const refusal = (message: string, details: Record<string, unknown>) =>
  new IpcError('permission.denied', message, false, details);

describe('a refusal while Nox controls a PC', () => {
  it.each([
    ['node_session_linked', 'reason_node_session_linked'],
    ['node_session_remote', 'reason_node_session_remote'],
    ['node_session_no_pin', 'reason_node_session_no_pin'],
    ['node_session_saturated', 'reason_node_session_saturated'],
  ] as const)('words the second reason %s', (sub, key) => {
    // Wherever the core puts the second word, and whatever it says next to it.
    for (const details of [{ reason: sub }, { reason: 'node_session', detail: sub }, { reason: 'node_session', sub_reason: sub }]) {
      expect(refusalText(t, refusal(NO_PIN, details), true, t('perm_reply_failed'))).toBe(t(key));
    }
  });

  it.each([
    [LINKED, 'reason_node_session_linked'],
    [REMOTE, 'reason_node_session_remote'],
    [NO_PIN, 'reason_node_session_no_pin'],
    [SATURATED, 'reason_node_session_saturated'],
  ] as const)('reads an older core by its sentence (%#)', (message, key) => {
    for (const pinSent of [false, true]) {
      const text = refusalText(t, refusal(message, { reason: 'node_session' }), pinSent, t('perm_reply_failed'));
      expect(text).toBe(t(key));
      expect(text).not.toBe(t('reason_pin_wrong'));
      expect(text).not.toBe(t('perm_reply_failed'));
    }
  });

  it('keeps a wrong PIN a wrong PIN, and anything else in German', () => {
    expect(refusalText(t, refusal('wrong PIN', { reason: 'node_session' }), true)).toBe(t('pin_wrong'));
    expect(refusalText(t, refusal('this client cannot approve now', { reason: 'node_session' }), true)).toBe(
      t('reason_node_session'),
    );
  });

  it('is never a PIN message where a page reads the PIN by itself', () => {
    expect(pinErrorKey('permission.denied', NO_PIN, true)).toBeNull();
    expect(pinErrorKey('permission.denied', 'wrong PIN', true)).toBe('pin_wrong');
  });

  it('shows in the permission prompt in the sentence for it', async () => {
    const prompt: PermissionPrompt = {
      id: 'g1',
      agent: 'companion',
      tool: 'file',
      action: 'delete',
      target: 'Archiv/neu.zip',
      risk: 'high',
      deadline: null,
      rememberTtlS: 0,
    };
    const client = {
      request: vi.fn(() => Promise.reject(refusal(LINKED, { reason: 'node_session', detail: 'node_session_linked' }))),
    } as unknown as IpcClient;
    render(
      <PermissionPromptDialog
        t={t}
        client={client}
        queue={[prompt]}
        device={false}
        pinSet
        nodeControl
        onAnswered={() => undefined}
        armDelayMs={0}
      />,
    );
    fireEvent.change(screen.getByLabelText(t('perm_pin')), { target: { value: '2468' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('perm_allow') }));
    });
    expect(screen.getByRole('alert').textContent).toBe(t('reason_node_session_linked'));
  });
});
