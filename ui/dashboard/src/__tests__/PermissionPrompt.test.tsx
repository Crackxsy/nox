/**
 * The permission prompt: what a person sees and what each answer sends to the core.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import type { IpcClient } from '../ipc';
import type { PermissionPrompt } from '../model';
import { PermissionPromptDialog } from '../PermissionPrompt';

const t = translator('de');
const PROMPT: PermissionPrompt = {
  id: 'g1',
  agent: 'nox',
  tool: 'file',
  action: 'delete',
  target: 'C:/tmp/report.txt',
  risk: 'high',
};

function recorder() {
  const calls: { name: string; payload: unknown }[] = [];
  const client = {
    request: vi.fn((name: string, payload: unknown) => {
      calls.push({ name, payload });
      return Promise.resolve({ ok: true });
    }),
  } as unknown as IpcClient;
  return { client, calls };
}

function show(props: Partial<Parameters<typeof PermissionPromptDialog>[0]> = {}) {
  const { client, calls } = recorder();
  const answered = vi.fn();
  render(
    <PermissionPromptDialog
      t={t}
      client={client}
      queue={[PROMPT]}
      device={false}
      decidedElsewhere={false}
      onAnswered={answered}
      {...props}
    />,
  );
  return { calls, answered };
}

describe('the permission prompt', () => {
  it('is a labelled alert dialog that says what, where and how risky', () => {
    show();
    const dialog = screen.getByRole('alertdialog', { name: t('perm_title') });
    // Words first, the identifier as a detail beneath them.
    expect(dialog.textContent).toContain(t('audit_tool_file'));
    expect(dialog.textContent).toContain(t('audit_action_delete'));
    expect(dialog.textContent).toContain('delete');
    expect(dialog.textContent).toContain('C:/tmp/report.txt');
    expect(dialog.textContent).toContain(t('perm_risk_high'));
  });

  it('puts focus on the answer that cannot do harm', () => {
    show();
    expect(document.activeElement?.textContent).toBe(t('perm_deny'));
  });

  it('denies on Escape', async () => {
    const { calls, answered } = show();
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    expect(calls).toEqual([
      { name: 'security.permission.reply', payload: { grant_id: 'g1', decision: 'deny', remember: false } },
    ]);
    expect(answered).toHaveBeenCalledWith('g1');
  });

  it('lets the local dashboard allow for good', async () => {
    const { calls } = show();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('perm_always') }));
    });
    expect(calls[0]?.payload).toEqual({ grant_id: 'g1', decision: 'allow', remember: true });
  });

  it('never offers a paired device a lasting grant', async () => {
    const { calls } = show({ device: true });
    expect(screen.queryByRole('button', { name: t('perm_always') })).toBeNull();
    expect(screen.getByText(t('perm_device_note')).textContent).toBe(t('perm_device_note'));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('perm_allow') }));
    });
    expect(calls[0]?.payload).toEqual({ grant_id: 'g1', decision: 'allow', remember: false });
  });

  it('says how many more are waiting', () => {
    show({ queue: [PROMPT, { ...PROMPT, id: 'g2' }, { ...PROMPT, id: 'g3' }] });
    expect(screen.getByText('2 weitere warten').textContent).toBe('2 weitere warten');
  });

  it('tells this device when another one decided', () => {
    show({ queue: [], decidedElsewhere: true });
    expect(screen.getByRole('status').textContent).toBe(t('perm_decided_elsewhere'));
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });
});
