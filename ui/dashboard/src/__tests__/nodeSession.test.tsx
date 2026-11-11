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
import type { PermissionPrompt } from '../model';
import { PermissionPromptDialog } from '../PermissionPrompt';

const t = translator('de');

// The core's sentences today (`nox.node.world_routes`), made up nowhere else.
const NO_PIN =
  'Nox controls mouse and keyboard on a paired PC; approving or changing anything now needs the security PIN, and none is set - deny, stop, or set a PIN first';

const refusal = (message: string, details: Record<string, unknown>) =>
  new IpcError('permission.denied', message, false, details);

describe('a refusal while Nox controls a PC', () => {
  it.each([
    ['node_session_linked', 'reason_node_session_linked'],
    ['node_session_remote', 'reason_node_session_remote'],
    ['node_session_no_pin', 'reason_node_session_no_pin'],
    ['node_session_saturated', 'reason_node_session_saturated'],
  ] as const)('words the second reason %s', (sub, key) => {
    const text = refusalText(t, refusal(NO_PIN, { reason: 'node_session', sub_reason: sub }), true, t('perm_reply_failed'));
    expect(text).toBe(t(key));
    expect(text).not.toBe(t('reason_pin_wrong'));
  });

  it('words role and pin too, and an older core without a sub-reason in German', () => {
    const pin = refusal('x', { reason: 'node_session', sub_reason: 'node_session_pin' });
    expect(refusalText(t, pin, true)).toBe(t('reason_pin_wrong'));
    const role = refusal('x', { reason: 'node_session', sub_reason: 'node_session_role' });
    expect(refusalText(t, role, true)).toBe(t('reason_node_session_role'));
    const old = refusal(NO_PIN, { reason: 'node_session' });
    expect(refusalText(t, old, true)).toBe(t('reason_node_session'));
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
    const onAnswered = vi.fn();
    const client = {
      // A refused "allow" is an answer, not an error (`security.permission.reply`).
      request: vi.fn(() => Promise.resolve({ ok: false, reason: NO_PIN, sub_reason: 'node_session_linked' })),
    } as unknown as IpcClient;
    render(
      <PermissionPromptDialog
        t={t}
        client={client}
        queue={[prompt]}
        device={false}
        pinSet
        nodeControl
        onAnswered={onAnswered}
        armDelayMs={0}
      />,
    );
    fireEvent.change(screen.getByLabelText(t('perm_pin')), { target: { value: '2468' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('perm_allow') }));
    });
    expect(screen.getByRole('alert').textContent).toBe(t('reason_node_session_linked'));
    expect(onAnswered).not.toHaveBeenCalled(); // the question stays
  });
});
