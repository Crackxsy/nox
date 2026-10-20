/**
 * The permission prompt: what a person sees, what each answer sends to the core, and how it
 * behaves at the edges - no connection, a reply in flight, a wrong PIN, the clock running out.
 */

import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';
import type { PermissionPrompt } from '../model';
import { PermissionNote } from '../PermissionNote';
import { PermissionPromptDialog } from '../PermissionPrompt';

const t = translator('de');
const NOW = Date.parse('2026-10-19T10:00:00Z');
const PROMPT: PermissionPrompt = {
  id: 'g1',
  agent: 'companion',
  tool: 'file',
  action: 'delete',
  target: 'C:/tmp/report.txt',
  risk: 'high',
  deadline: NOW + 42_000,
  rememberTtlS: 8 * 3600,
};

type Answer = (name: string, payload: unknown) => Promise<unknown>;

function recorder(answer: Answer = () => Promise.resolve({ ok: true })) {
  const calls: { name: string; payload: unknown }[] = [];
  const client = {
    request: vi.fn((name: string, payload: unknown) => {
      calls.push({ name, payload });
      return answer(name, payload);
    }),
  } as unknown as IpcClient;
  return { client, calls };
}

function show(
  props: Partial<Parameters<typeof PermissionPromptDialog>[0]> = {},
  answer?: Answer,
) {
  const { client, calls } = recorder(answer);
  const answered = vi.fn();
  const view = render(
    <PermissionPromptDialog
      t={t}
      client={client}
      queue={[PROMPT]}
      device={false}
      pinSet={null}
      onAnswered={answered}
      clock={() => NOW}
      {...props}
    />,
  );
  return { calls, answered, client, view };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('what the prompt says', () => {
  it('asks in one plain sentence first, then the area and the risk in words', () => {
    show();
    // The heading is a yes/no question in everyday words, by action.
    const dialog = screen.getByRole('alertdialog', { name: 'Darf Nox etwas löschen?' });
    const sentence = within(dialog).getByText('Nox möchte löschen: C:/tmp/report.txt');
    // The sentence comes right after the question.
    expect(dialog.querySelector('.ask-head + p')).toBe(sentence);
    expect(dialog.getAttribute('aria-describedby')).toContain(sentence.id);
    expect(dialog.textContent).toContain(t('audit_tool_file'));
    expect(dialog.textContent).toContain(t('perm_risk_high'));
  });

  it('keeps the identifiers behind "technical details"', () => {
    show();
    const details = screen.getByText(t('perm_technical')).closest('details')!;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain('file');
    expect(details.textContent).toContain('delete');
    expect(details.textContent).toContain('companion');
  });

  it('never says "always"', () => {
    show();
    expect(document.body.textContent).not.toMatch(/immer|always/i);
  });

  it('says how many more are waiting', () => {
    show({ queue: [PROMPT, { ...PROMPT, id: 'g2' }, { ...PROMPT, id: 'g3' }] });
    expect(screen.getByText('2 weitere warten').textContent).toBe('2 weitere warten');
  });
});

describe('the answers', () => {
  it('offers Allow, Deny and the quiet timed allow, with focus on Deny (the safe answer)', () => {
    show();
    const dialog = screen.getByRole('alertdialog');
    const labels = within(dialog)
      .getAllByRole('button')
      .map((b) => b.textContent);
    expect(labels).toEqual([t('perm_allow'), t('perm_deny'), 'Für 8 Stunden erlauben']);
    expect(document.activeElement?.textContent).toBe(t('perm_deny'));
  });

  it('falls back to "Darf Nox das?" for an action it has no question for', () => {
    show({ queue: [{ ...PROMPT, action: 'frobnicate' }] });
    expect(screen.getByRole('alertdialog', { name: t('perm_title') })).toBeTruthy();
  });

  it('does not dress Allow as the inviting primary button at high risk', () => {
    show();
    const allow = screen.getByRole('button', { name: t('perm_allow') });
    expect(allow.className).toContain('btn--risky');
  });

  it('keeps Allow the primary button at medium risk', () => {
    show({ queue: [{ ...PROMPT, risk: 'medium' }] });
    expect(screen.getByRole('button', { name: t('perm_allow') }).className).toBe('btn btn--primary');
  });

  it('lets the local dashboard allow for as long as the core keeps such a grant', async () => {
    const { calls } = show();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Für 8 Stunden erlauben' }));
    });
    expect(calls[0]?.payload).toEqual({ grant_id: 'g1', decision: 'allow', remember: true });
  });

  it('offers no timed allow when the core does not say for how long', () => {
    show({ queue: [{ ...PROMPT, rememberTtlS: 0 }] });
    expect(screen.queryByRole('button', { name: /Stunde|Minute/ })).toBeNull();
  });
});

describe('Escape', () => {
  it('denies once, also when pressed again while the answer is on its way', async () => {
    let release: (value: unknown) => void = () => undefined;
    const { calls, answered } = show({}, () => new Promise((resolve) => (release = resolve)));
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' });
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    expect(calls).toEqual([
      { name: 'security.permission.reply', payload: { grant_id: 'g1', decision: 'deny', remember: false } },
    ]);
    await act(async () => release({ ok: true }));
    expect(answered).toHaveBeenCalledWith('g1', 'deny');
  });

  it('says so when the denial did not arrive', async () => {
    const { answered } = show({}, () => Promise.reject(new IpcError('unavailable', 'socket closed', true)));
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    expect(screen.getByRole('alert').textContent).toBe(t('perm_reply_failed'));
    expect(answered).not.toHaveBeenCalled();
  });
});

describe('without a connection', () => {
  it('says that answering is not possible, and keeps focus in the dialog', () => {
    show({ client: null });
    const dialog = screen.getByRole('alertdialog');
    expect(within(dialog).getByText(t('perm_offline')).textContent).toBe(t('perm_offline'));
    expect(document.activeElement).toBe(dialog);
    // Tab moves between what can still be reached inside, never to the page behind.
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});

describe('on a paired device', () => {
  it('never offers a lasting grant, and says an answer counts once', async () => {
    const { calls } = show({ device: true, queue: [{ ...PROMPT, risk: 'medium' }] });
    expect(screen.queryByRole('button', { name: /Stunde/ })).toBeNull();
    expect(screen.getByText(t('perm_device_note')).textContent).toBe(t('perm_device_note'));
    expect(screen.queryByLabelText(t('perm_pin'))).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('perm_allow') }));
    });
    expect(calls[0]?.payload).toEqual({ grant_id: 'g1', decision: 'allow', remember: false });
  });

  it('asks for the PIN at high risk and sends it with the answer', async () => {
    const { calls } = show({ device: true, pinSet: true });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('perm_allow') }));
    });
    expect(calls).toEqual([]);
    expect(screen.getByRole('alert').textContent).toBe(t('perm_pin_missing'));
    fireEvent.change(screen.getByLabelText(t('perm_pin')), { target: { value: '4711' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('perm_allow') }));
    });
    expect(calls[0]?.payload).toEqual({ grant_id: 'g1', decision: 'allow', remember: false, pin: '4711' });
  });

  it('says a wrong PIN in words, and what three of them do', async () => {
    show({ device: true, pinSet: true }, () =>
      Promise.reject(new IpcError('permission.denied', 'denied: wrong PIN', false, { reason: 'pin_wrong' })),
    );
    fireEvent.change(screen.getByLabelText(t('perm_pin')), { target: { value: '0000' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('perm_allow') }));
    });
    expect(screen.getByRole('alert').textContent).toBe(t('reason_pin_wrong'));
    expect((screen.getByLabelText(t('perm_pin')) as HTMLInputElement).value).toBe('');
  });

  it('without a PIN set, does not offer Allow at high risk but still lets it deny', async () => {
    const { calls } = show({ device: true, pinSet: false });
    expect(screen.queryByRole('button', { name: t('perm_allow') })).toBeNull();
    expect(screen.getByText(t('perm_only_at_computer')).textContent).toBe(t('perm_only_at_computer'));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('perm_deny') }));
    });
    expect(calls[0]?.payload).toEqual({ grant_id: 'g1', decision: 'deny', remember: false });
  });
});

describe('the countdown', () => {
  it('shows the seconds left and announces only 30 and 10', () => {
    vi.useFakeTimers();
    let now = NOW;
    show({ clock: () => now });
    expect(screen.getByText('Noch 42 Sekunden. Ohne Antwort tut Nox es nicht.')).toBeTruthy();
    // The risk as a word, right before the time left.
    expect(screen.getByText('Risiko hoch')).toBeTruthy();
    const live = document.querySelector('.perm-dialog [aria-live="polite"]')!;
    expect(live.textContent).toBe('');
    const tick = (seconds: number) => {
      for (let i = 0; i < seconds; i += 1) {
        now += 1000;
        act(() => {
          vi.advanceTimersByTime(1000);
        });
      }
    };
    tick(5); // 37 left
    expect(live.textContent).toBe('');
    tick(7); // 30 left
    expect(live.textContent).toBe('Noch 30 Sekunden für eine Antwort.');
    tick(5); // 25 left: no new announcement
    expect(live.textContent).toBe('Noch 30 Sekunden für eine Antwort.');
    tick(15); // 10 left
    expect(live.textContent).toBe('Noch 10 Sekunden für eine Antwort.');
    tick(15);
    expect(screen.getByText(t('perm_countdown_over'))).toBeTruthy();
  });
});

describe('how a question ended elsewhere', () => {
  it('keeps an empty live region in the page, then says what happened', () => {
    const { rerender } = render(<PermissionNote t={t} note={null} />);
    const region = screen.getByRole('status');
    expect(region.textContent).toBe('');
    rerender(<PermissionNote t={t} note="timeout" />);
    expect(screen.getByRole('status')).toBe(region);
    expect(region.textContent).toBe('Keine Antwort – Nox hat es nicht getan.');
    rerender(<PermissionNote t={t} note="elsewhere" />);
    expect(region.textContent).toBe(t('perm_decided_elsewhere'));
  });
});
